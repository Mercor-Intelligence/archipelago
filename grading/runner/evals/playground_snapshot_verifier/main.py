"""Playground snapshot verifier - compares agent state against golden state.

Uses normalization to strip volatile fields (timestamps, auto-IDs) before comparison,
with LLM fallback for edge cases where normalized content still differs.
"""

import io

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from runner.evals.models import EvalImplInput
from runner.evals.utils.s3 import (
    download_s3_file,
    download_s3_prefix_as_zip,
    is_s3_prefix_uri,
)
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages
from runner.utils.llm_judge import LLM_JUDGE_TIMEOUT, call_llm_judge

from .diff import (
    analyze_diff_severity,
    compute_normalized_diff,
    format_diff_for_prompt,
    has_differences,
)
from .extraction import extract_and_normalize_files
from .models import PlaygroundSnapshotJudgeResponse

# Truncation limit for diff summary stored in result
MAX_STORED_DIFF_SIZE = 5000

SYSTEM_PROMPT = """You are comparing an AI agent's final state against the expected (golden) state.

Both states have been normalized to remove volatile fields (timestamps, auto-generated IDs, session tokens, password hashes). Any remaining differences are potentially meaningful.

## Classification Rules

BUSINESS DATA differences - these should almost always FAIL:
- Customer, product, order, invoice, or any domain entity data
- Names, descriptions, prices, quantities, statuses
- Any table that doesn't start with underscore (e.g., tabCustomer, tabProduct, tabSalesOrder)
- Configuration that affects business logic

SYSTEM/INFRASTRUCTURE differences - these MAY be trivial:
- Tables starting with underscore (__Auth, __Settings, __SessionCache, etc.)
- Internal framework tables (tabDocType, tabDocField, tabSingles)
- Formatting differences (whitespace, key ordering in JSON)
- Empty vs null values in non-business fields

## Decision Framework

1. First, identify what type of data differs (business vs system)
2. For BUSINESS DATA differences:
   - Different values in business fields (names, amounts, etc.) → FAIL
   - Missing or extra business records → FAIL
   - Wrong status or state in business entities → FAIL
3. For SYSTEM differences:
   - Only if ALL differences are in system tables AND don't affect functionality → PASS
   - If unsure whether a table is business or system → treat as business → FAIL

## Critical Rule
When in doubt, FAIL. It is better to flag a false positive than to miss actual data corruption.

Return JSON: {"result": 1, "reason": "..."} for pass, {"result": 0, "reason": "..."} for fail.
Your reason should explicitly state what type of differences you found and why you classified them as trivial or meaningful."""


async def _download_golden_snapshot(s3_url: str) -> io.BytesIO:
    """Download golden snapshot from S3 URL.

    Supports:
    - S3 prefix URIs (s3://bucket/prefix/) - downloads all files and creates ZIP
    - Presigned HTTPS URLs - downloads single file directly

    Playground snapshots are stored as individual files under an S3 prefix,
    not as a single archive file. This function handles both formats.

    Args:
        s3_url: S3 URI (prefix or file) or presigned HTTPS URL

    Returns:
        BytesIO containing a ZIP archive of the snapshot files
    """
    if s3_url.startswith("s3://"):
        # S3 URI - check if it's a prefix (directory) or single file
        if is_s3_prefix_uri(s3_url):
            # Playground snapshots are stored as individual files under a prefix
            # Download all files and package as ZIP
            logger.info(
                f"[PLAYGROUND_SNAPSHOT_VERIFIER] Downloading S3 prefix: {s3_url}"
            )
            return await download_s3_prefix_as_zip(s3_url)
        else:
            # Single file (e.g., pre-packaged ZIP) - download directly
            logger.info(f"[PLAYGROUND_SNAPSHOT_VERIFIER] Downloading S3 file: {s3_url}")
            return await download_s3_file(s3_url)
    else:
        # Presigned HTTPS URL - download single file directly
        logger.info("[PLAYGROUND_SNAPSHOT_VERIFIER] Downloading presigned URL")

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        )
        async def _download_presigned() -> io.BytesIO:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.get(s3_url)
                response.raise_for_status()
                return io.BytesIO(response.content)

        return await _download_presigned()


async def playground_snapshot_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Playground Snapshot Verifier - Compare agent state against golden state.

    This verifier:
    1. Downloads the golden snapshot from S3
    2. Extracts and normalizes all files from both snapshots
       (strips volatile fields: timestamps, auto-IDs, session tokens)
    3. Compares normalized content (databases, JSON, text files)
    4. If exact match after normalization → PASS
    5. If differences remain → LLM judges if they're meaningful

    Config fields:
    - file: S3 URL to golden snapshot (required)
    - playground_number: Identifier for tracking (optional)
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"
    playground_number = verifier_values.get("playground_number")

    snapshot_prefix = verifier_values.get("snapshot_prefix", "").rstrip("/")
    snapshot_id = verifier_values.get("file", "")
    # Construct full S3 URL from prefix + snapshot ID, or use file as-is if it's already a full URL
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
        logger.error(f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | {error_msg}")
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
        f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | "
        f"playground={playground_number} | starting verification"
    )

    try:
        # 1. Download golden snapshot
        golden_bytes = await _download_golden_snapshot(s3_url)

        # 2. Extract and normalize all files from both snapshots
        logger.info(
            f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | "
            f"extracting and normalizing files"
        )
        golden_result = extract_and_normalize_files(golden_bytes)
        agent_result = extract_and_normalize_files(input.final_snapshot_bytes)

        # Check for fatal extraction errors
        if golden_result.had_fatal_error:
            error_msg = f"Failed to extract golden snapshot: {golden_result.errors}"
            logger.error(f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | {error_msg}")
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
            logger.error(f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | {error_msg}")
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

        # Log skipped binary files and non-fatal errors
        all_skipped = golden_result.skipped_binary + agent_result.skipped_binary
        all_errors = golden_result.errors + agent_result.errors
        if all_skipped:
            logger.info(
                f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | "
                f"skipped {len(all_skipped)} binary files"
            )
        if all_errors:
            logger.warning(
                f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | "
                f"extraction warnings: {all_errors}"
            )

        # Handle empty snapshots - no verification possible
        if not golden_files and not agent_files:
            error_msg = "No comparable files found in either snapshot"
            logger.error(f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | {error_msg}")
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

        # 3. Compare normalized content
        diff = compute_normalized_diff(golden_files, agent_files)

        # 4. Check if exact match after normalization
        if not has_differences(diff):
            logger.info(
                f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | "
                f"PASS - exact match after normalization ({len(diff.matches)} files)"
            )
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=1.0,
                verifier_result_values={
                    "judge_grade": "pass",
                    "reason": f"Exact match after normalization ({len(diff.matches)} files)",
                    "diff_summary": "No differences after normalizing volatile fields",
                    "playground_number": playground_number,
                    "file": s3_url,
                },
            )

        # 5. Differences remain - analyze severity and use LLM to judge
        severity_analysis = analyze_diff_severity(diff)
        diff_summary = format_diff_for_prompt(diff)

        logger.info(
            f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | "
            f"found differences after normalization | "
            f"severity={severity_analysis['severity']} | "
            f"business_tables={severity_analysis['business_tables']} | "
            f"system_tables={severity_analysis['system_tables']} | "
            f"has_non_db_diffs={severity_analysis['has_non_db_diffs']}"
        )

        # Build context-aware prompt
        severity_context = ""
        if severity_analysis["severity"] == "high":
            severity_context = (
                "\n\nIMPORTANT: This diff contains differences in BUSINESS TABLES "
                f"({', '.join(severity_analysis['business_tables'])}). "
                "These differences are likely meaningful and should result in FAIL "
                "unless you can determine they are truly equivalent data representations."
            )
        elif (
            severity_analysis["severity"] == "low"
            and not severity_analysis["has_non_db_diffs"]
        ):
            severity_context = (
                "\n\nNOTE: All differences appear to be in SYSTEM/INFRASTRUCTURE tables "
                f"({', '.join(severity_analysis['system_tables'])}). "
                "These may be trivial, but verify no business logic is affected."
            )

        user_prompt = f"File comparison results:\n{diff_summary}{severity_context}"
        messages = build_messages(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)

        parsed_response = await call_llm_judge(
            model=input.grading_settings.llm_judge_model,
            messages=messages,
            response_class=PlaygroundSnapshotJudgeResponse,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=input.grading_settings.llm_judge_extra_args,
            log_prefix="PLAYGROUND_SNAPSHOT_VERIFIER",
        )

        passed = parsed_response.result == 1
        logger.info(
            f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | "
            f"{'PASS' if passed else 'FAIL'} - {parsed_response.reason[:100]}"
        )

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=1.0 if passed else 0.0,
            verifier_result_values={
                "judge_grade": "pass" if passed else "fail",
                "reason": parsed_response.reason,
                "diff_summary": diff_summary[:MAX_STORED_DIFF_SIZE],
                "diff_severity": severity_analysis["severity"],
                "business_tables_with_diffs": severity_analysis["business_tables"],
                "system_tables_with_diffs": severity_analysis["system_tables"],
                "playground_number": playground_number,
                "file": s3_url,
            },
        )

    except Exception as e:
        error_msg = f"Playground snapshot verification failed: {str(e)}"
        logger.error(f"[PLAYGROUND_SNAPSHOT_VERIFIER] task={task_id} | {error_msg}")
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
