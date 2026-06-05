"""Playground snapshot judge - task-aware parallel LLM judge.

Compares agent state against golden state using per-table parallel LLM calls,
each informed by the task description so the judge can reason about whether
a diff is noise, task_incomplete, or unwanted_mutation.
"""

import asyncio
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import httpx
from litellm import Choices
from loguru import logger
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from runner.evals.models import EvalImplInput
from runner.evals.output_llm.utils.shared import extract_task_prompt
from runner.evals.utils.s3 import (
    download_s3_file,
    download_s3_prefix_as_zip,
    is_s3_prefix_uri,
)
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages, call_llm
from runner.utils.llm_judge import LLM_JUDGE_TIMEOUT, JudgeResponse, call_llm_judge

from .diff import (
    _is_system_table,
    analyze_diff_severity,
    compute_normalized_diff,
    extract_per_table_diffs,
    has_differences,
)
from .extraction import extract_and_normalize_files
from .models import NormalizedDiff, PlaygroundSnapshotJudgeResponse, TableVerdict

MAX_STORED_DIFF_SIZE = 5000

_SYSTEM_PROMPT_TABLE = """You are analyzing a single table's diff between an AI agent's final application state and the expected (golden) state after completing a task.

The golden state was captured by a human completing the task correctly. Timestamps, session tokens, auto-IDs, and other known volatile fields have already been stripped by normalization.

YOUR ONLY JOB: determine whether the remaining difference in THIS TABLE is meaningful or noise.

━━━ CLASSIFICATIONS ━━━

noise
  Infrastructure or system churn that varies between correct runs regardless of task.
  The same diff would appear even if the task was done perfectly twice.
  Examples: session token regeneration, job queue entries, distributed lock timestamps,
  cache invalidation counters, background sync tokens, migration version rows.

task_incomplete
  Business data that diverges from the golden state. The agent failed to reach the
  correct final state — something is missing, wrong, or different from what a correct
  run produces.

unwanted_mutation
  Business data that changed in the agent state but was NOT changed in the golden state.
  The agent modified something it shouldn't have — this table/data had nothing to do
  with the task.

━━━ DECISION RULES ━━━

1. Reason from the task description, not just the table name.
2. The same table type can be noise for one task and task_incomplete for another.
3. When uncertain whether a diff is noise or meaningful → treat as meaningful.

━━━ EXAMPLES ━━━

Example 1 — noise (session state)
Task: "Add a product to the shopping cart"
Table: user_sessions
  Golden: 2 rows (session tokens: abc123, def456)
  Agent:  2 rows (session tokens: xyz789, uvw012)
→ noise. Session tokens regenerate on every authentication regardless of action taken.

Example 2 — noise (job queue side effect)
Task: "Send a password reset email to user alice@example.com"
Table: background_jobs
  Golden: rows for jobs [id=914, id=915] status=completed
  Agent:  rows for jobs [id=201, id=202] status=pending
→ noise. Job IDs and status vary between runs even for identical work.

Example 3 — task_incomplete (same table, different task)
Task: "Schedule a background job to reindex all documents"
Table: background_jobs
  Golden: 1 row — type=reindex_documents, status=queued
  Agent:  0 rows of type reindex_documents
→ task_incomplete. The job entry IS the task outcome; its absence means task was not done.

Example 4 — task_incomplete (missing business record)
Task: "Create a new contact named John Smith with email john@example.com"
Table: contacts
  Golden: 1 new row — name="John Smith", email="john@example.com"
  Agent:  no new rows
→ task_incomplete. The required record is absent.

Example 5 — unwanted_mutation
Task: "Create a new contact named John Smith"
Table: user_settings
  Golden: admin user settings unchanged
  Agent:  admin user notification_email changed from "admin@co.com" to ""
→ unwanted_mutation. Settings unrelated to the task were modified.

━━━ OUTPUT FORMAT ━━━

Return valid JSON only:
{"classification": "noise" | "task_incomplete" | "unwanted_mutation", "reason": "<one sentence>"}"""


_SYSTEM_PROMPT_AGGREGATE = """You are producing a final pass/fail verdict for an AI agent task completion check.

You have per-table analysis results from a diff of the agent's final state against the expected (golden) state.

RULES:
- Any task_incomplete or unwanted_mutation → fail
- All noise → pass
- In your reason: name the specific tables/entities that caused a fail (in plain English, not raw table names). If passing, briefly note what infrastructure noise was safely ignored.

Return valid JSON only:
{"result": 1, "reason": "..."} for pass, {"result": 0, "reason": "..."} for fail.
2–3 sentences max in reason."""


async def _judge_unit(
    model: str,
    extra_args: dict[str, Any] | None,
    task_prompt: str,
    all_units_summary: str,
    unit_name: str,
    added: list[str],
    removed: list[str],
) -> tuple[str, TableVerdict]:
    """Call LLM to classify a single diff unit (table or file).

    Returns (unit_name, TableVerdict).
    """
    added_section = "\n".join(added) if added else "(none)"
    removed_section = "\n".join(removed) if removed else "(none)"

    user_prompt = f"""Task description:
{task_prompt}

All differing tables/files in this snapshot (for context):
{all_units_summary}

━━━ Table/file to classify: {unit_name} ━━━

Rows/content in agent but NOT in golden (added by agent):
{added_section}

Rows/content in golden but NOT in agent (missing from agent):
{removed_section}"""

    messages = build_messages(
        system_prompt=_SYSTEM_PROMPT_TABLE, user_prompt=user_prompt
    )

    for attempt in range(10):
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format={"type": "json_object"},
        )
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            continue
        raw = choices[0].message.content
        if not raw:
            continue
        try:
            # Gemini quirk: reason may be returned as a dict instead of str
            try:
                raw_json = json.loads(raw)
                if isinstance(raw_json, list) and len(raw_json) == 1:
                    raw_json = raw_json[0]
                    raw = json.dumps(raw_json)
                if isinstance(raw_json, dict) and isinstance(
                    raw_json.get("reason"), dict
                ):
                    raw_json["reason"] = json.dumps(raw_json["reason"])
                    raw = json.dumps(raw_json)
            except json.JSONDecodeError:
                pass
            verdict = TableVerdict.model_validate_json(raw)
            return unit_name, verdict
        except (ValidationError, json.JSONDecodeError):
            logger.warning(
                f"[PLAYGROUND_SNAPSHOT_JUDGE] per-unit retry {attempt + 1}/10 "
                f"for {unit_name}"
            )

    # Fallback: treat as task_incomplete (safe default)
    return unit_name, TableVerdict(
        classification="task_incomplete",
        reason="Could not parse LLM response; treating as meaningful difference",
    )


async def _judge_with_task_context(
    input: EvalImplInput,
    task_prompt: str,
    diff: NormalizedDiff,
) -> PlaygroundSnapshotJudgeResponse:
    """Run parallel per-table LLM calls then aggregate into final verdict."""
    from .diff import _is_db_file

    # Build all diff units: one per table (for DB files) or one per file (others)
    units: dict[str, tuple[list[str], list[str]]] = {}

    for mismatch in diff.mismatches:
        if _is_db_file(mismatch.file_path):
            golden = mismatch.golden_full or mismatch.golden_preview
            agent = mismatch.agent_full or mismatch.agent_preview
            table_diffs = extract_per_table_diffs(golden, agent)
            for table_name, td in table_diffs.items():
                units[table_name] = (td["added"], td["removed"])
            # If no tables extracted, treat the whole file as one unit
            if not table_diffs:
                units[mismatch.file_path] = (
                    [mismatch.agent_preview[:2000]],
                    [mismatch.golden_preview[:2000]],
                )
        else:
            units[mismatch.file_path] = (
                [mismatch.agent_preview[:2000]],
                [mismatch.golden_preview[:2000]],
            )

    # Missing/extra files: one unit each
    for path in diff.missing_in_agent:
        units[f"[missing] {path}"] = ([], [f"File existed in golden: {path}"])
    for path in diff.extra_in_agent:
        units[f"[extra] {path}"] = ([f"File added by agent: {path}"], [])

    if not units:
        # No units to judge — this shouldn't happen (caller checks has_differences)
        return PlaygroundSnapshotJudgeResponse(
            result=1, reason="No differences to evaluate"
        )

    # Build compact summary of all differing units for cross-table context
    all_units_summary_lines = []
    for name, (added, removed) in units.items():
        all_units_summary_lines.append(
            f"  {name}: +{len(added)} rows/lines, -{len(removed)} rows/lines"
        )
    all_units_summary = "\n".join(all_units_summary_lines)

    # Fast path: all units are system tables → skip LLM entirely
    non_system_units = [
        name
        for name in units
        if not _is_system_table(name.split(".")[-1] if "." in name else name)
    ]
    if not non_system_units and not diff.missing_in_agent and not diff.extra_in_agent:
        return PlaygroundSnapshotJudgeResponse(
            result=1,
            reason="All differences are in system/infrastructure tables; treated as noise.",
            table_verdicts={
                name: TableVerdict(
                    classification="noise",
                    reason="System/infrastructure table; skipped LLM call",
                )
                for name in units
            },
        )

    model = input.grading_settings.llm_judge_model
    extra_args = input.grading_settings.llm_judge_extra_args

    # Parallel per-unit calls
    tasks = [
        _judge_unit(
            model=model,
            extra_args=extra_args,
            task_prompt=task_prompt,
            all_units_summary=all_units_summary,
            unit_name=name,
            added=added,
            removed=removed,
        )
        for name, (added, removed) in units.items()
    ]
    results = await asyncio.gather(*tasks)
    table_verdicts = dict(results)

    # Build aggregation prompt
    verdicts_lines = "\n".join(
        f"{name}: {v.classification} — {v.reason}" for name, v in table_verdicts.items()
    )
    agg_user_prompt = f"""Task: {task_prompt}

Per-table verdicts:
{verdicts_lines}"""

    agg_messages = build_messages(
        system_prompt=_SYSTEM_PROMPT_AGGREGATE, user_prompt=agg_user_prompt
    )

    agg_response = await call_llm_judge(
        model=model,
        messages=agg_messages,
        response_class=JudgeResponse,
        timeout=LLM_JUDGE_TIMEOUT,
        extra_args=extra_args,
        log_prefix="PLAYGROUND_SNAPSHOT_JUDGE",
    )

    return PlaygroundSnapshotJudgeResponse(
        result=agg_response.result,
        reason=agg_response.reason,
        table_verdicts=table_verdicts,
    )


async def _download_golden_snapshot(s3_url: str) -> io.BytesIO:
    if os.path.isdir(s3_url):
        logger.info(
            f"[PLAYGROUND_SNAPSHOT_JUDGE] Loading local golden snapshot: {s3_url}"
        )
        return _zip_local_directory(s3_url)
    if os.path.isfile(s3_url):
        logger.info(f"[PLAYGROUND_SNAPSHOT_JUDGE] Loading local file: {s3_url}")
        return io.BytesIO(Path(s3_url).read_bytes())
    if s3_url.startswith("s3://"):
        if is_s3_prefix_uri(s3_url):
            logger.info(f"[PLAYGROUND_SNAPSHOT_JUDGE] Downloading S3 prefix: {s3_url}")
            return await download_s3_prefix_as_zip(s3_url)
        else:
            logger.info(f"[PLAYGROUND_SNAPSHOT_JUDGE] Downloading S3 file: {s3_url}")
            return await download_s3_file(s3_url)
    else:
        logger.info("[PLAYGROUND_SNAPSHOT_JUDGE] Downloading presigned URL")

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        )
        async def _download() -> io.BytesIO:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.get(s3_url)
                response.raise_for_status()
                return io.BytesIO(response.content)

        return await _download()


def _zip_local_directory(path: str) -> io.BytesIO:
    """Zip a local directory into a BytesIO, matching the S3 download format."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        root = Path(path)
        for file in root.rglob("*"):
            if file.is_file():
                zf.writestr(str(file.relative_to(root)), file.read_bytes())
    buf.seek(0)
    return buf


async def playground_snapshot_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Playground Snapshot Judge - task-aware parallel LLM judge.

    Like the snapshot verifier but each differing table gets its own LLM call
    with the task description, enabling noise/task_incomplete/unwanted_mutation
    classification. An aggregation call produces the final verdict and plain-English
    reason suitable for annotator review.
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"
    playground_number = verifier_values.get("playground_number")

    snapshot_prefix = verifier_values.get("snapshot_prefix", "").rstrip("/")
    snapshot_id = verifier_values.get("file", "")
    if (
        snapshot_id
        and not snapshot_id.startswith(("s3://", "http://", "https://"))
        and snapshot_prefix
    ):
        s3_url = f"{snapshot_prefix}/{snapshot_id}"
    else:
        s3_url = snapshot_id

    if not s3_url:
        error_msg = "Missing required field: file (S3 URL to golden snapshot)"
        logger.error(f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | {error_msg}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "playground_number": playground_number,
                "file": s3_url,
            },
            message=error_msg,
        )

    logger.info(
        f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | "
        f"playground={playground_number} | starting verification"
    )

    try:
        golden_bytes = await _download_golden_snapshot(s3_url)

        logger.info(
            f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | extracting and normalizing files"
        )
        golden_result = extract_and_normalize_files(golden_bytes)
        agent_result = extract_and_normalize_files(input.final_snapshot_bytes)

        if golden_result.had_fatal_error:
            error_msg = f"Failed to extract golden snapshot: {golden_result.errors}"
            logger.error(f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | {error_msg}")
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={
                    "playground_number": playground_number,
                    "file": s3_url,
                },
                message=error_msg,
            )
        if agent_result.had_fatal_error:
            error_msg = f"Failed to extract agent snapshot: {agent_result.errors}"
            logger.error(f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | {error_msg}")
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={
                    "playground_number": playground_number,
                    "file": s3_url,
                },
                message=error_msg,
            )

        golden_files = golden_result.normalized_files
        agent_files = agent_result.normalized_files

        all_skipped = golden_result.skipped_binary + agent_result.skipped_binary
        all_errors = golden_result.errors + agent_result.errors
        if all_skipped:
            logger.info(
                f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | "
                f"skipped {len(all_skipped)} binary files"
            )
        if all_errors:
            logger.warning(
                f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | extraction warnings: {all_errors}"
            )

        if not golden_files and not agent_files:
            error_msg = "No comparable files found in either snapshot"
            logger.error(f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | {error_msg}")
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={
                    "playground_number": playground_number,
                    "file": s3_url,
                    "extraction_warnings": all_errors if all_errors else None,
                },
                message=error_msg,
            )

        diff = compute_normalized_diff(golden_files, agent_files)

        if not has_differences(diff):
            logger.info(
                f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | "
                f"PASS - exact match after normalization ({len(diff.matches)} files)"
            )
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=1.0,
                verifier_result_values={
                    "judge_grade": "pass",
                    "reason": f"Exact match after normalization ({len(diff.matches)} files)",
                    "playground_number": playground_number,
                    "file": s3_url,
                },
            )

        # Extract task prompt from trajectory (auto-injected, no annotator work needed)
        task_prompt = extract_task_prompt(input)
        if not task_prompt:
            task_prompt = "(Task description not available)"
            logger.warning(
                f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | no task prompt found in trajectory"
            )

        severity_analysis = analyze_diff_severity(diff)
        logger.info(
            f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | "
            f"found differences | severity={severity_analysis['severity']} | "
            f"business_tables={severity_analysis['business_tables']} | "
            f"system_tables={severity_analysis['system_tables']}"
        )

        parsed_response = await _judge_with_task_context(
            input=input,
            task_prompt=task_prompt,
            diff=diff,
        )

        passed = parsed_response.result == 1
        logger.info(
            f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | "
            f"{'PASS' if passed else 'FAIL'} - {parsed_response.reason[:100]}"
        )

        table_verdicts_serialized = {
            name: {"classification": v.classification, "reason": v.reason}
            for name, v in parsed_response.table_verdicts.items()
        }

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=1.0 if passed else 0.0,
            verifier_result_values={
                "judge_grade": "pass" if passed else "fail",
                "reason": parsed_response.reason,
                "table_verdicts": table_verdicts_serialized,
                "diff_severity": severity_analysis["severity"],
                "business_tables_with_diffs": severity_analysis["business_tables"],
                "system_tables_with_diffs": severity_analysis["system_tables"],
                "playground_number": playground_number,
                "file": s3_url,
            },
        )

    except Exception as e:
        error_msg = f"Playground snapshot judge failed: {str(e)}"
        logger.error(f"[PLAYGROUND_SNAPSHOT_JUDGE] task={task_id} | {error_msg}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "playground_number": playground_number,
                "file": s3_url,
            },
            message=error_msg,
        )
