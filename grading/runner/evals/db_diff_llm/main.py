"""DB Diff LLM Judge - evaluates database changes against criteria using LLM."""

import json
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.llm import build_messages, call_llm

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

# Max retries for JSON validation errors (matches output_llm)
MAX_JSON_RETRIES = 10


class DbDiffJudgeResponse(BaseModel):
    """Response schema for DB diff LLM judge output."""

    result: int  # 1 = pass, 0 = fail
    reason: str  # Explanation for the judgment


# System prompt for DB diff evaluation
DB_DIFF_JUDGE_SYSTEM_PROMPT = """You are evaluating database changes made by an AI agent against specific criteria. You will be given:
1. A summary of database changes (rows added, deleted, and modified)
2. Criteria describing the expected database changes

Your task is to determine if the database changes satisfy the criteria and provide a concise explanation.

Return your evaluation as JSON with:
- "result": 1 if criteria is satisfied, 0 if not
- "reason": concise explanation (2-3 sentences max)"""


def _format_db_diff_for_prompt(db_diff_result: dict[str, Any]) -> str:
    """Format the DB diff result as a readable string for the LLM prompt.

    Args:
        db_diff_result: The diff result from db_diff helper
    """
    databases = db_diff_result.get("databases", {})

    # Build detail lines and compute summary
    detail_lines: list[str] = []
    total_added = 0
    total_deleted = 0
    total_modified = 0
    tables_changed: list[str] = []

    for db_path, db_data in databases.items():
        detail_lines.append(f"=== Database: {db_path} ===")
        tables = db_data.get("tables", {})

        for table_name, table_diff in tables.items():
            rows_added = table_diff.get("rows_added", [])
            rows_deleted = table_diff.get("rows_deleted", [])
            rows_modified = table_diff.get("rows_modified", [])

            # Skip tables with no changes
            if not rows_added and not rows_deleted and not rows_modified:
                continue

            # Update summary counts
            total_added += len(rows_added)
            total_deleted += len(rows_deleted)
            total_modified += len(rows_modified)
            tables_changed.append(f"{db_path}:{table_name}")

            detail_lines.append(f"\n--- Table: {table_name} ---")

            if rows_added:
                detail_lines.append(f"Rows Added ({len(rows_added)}):")
                for row in rows_added:
                    detail_lines.append(f"  {json.dumps(row)}")

            if rows_deleted:
                detail_lines.append(f"Rows Deleted ({len(rows_deleted)}):")
                for row in rows_deleted:
                    detail_lines.append(f"  {json.dumps(row)}")

            if rows_modified:
                detail_lines.append(f"Rows Modified ({len(rows_modified)}):")
                for modification in rows_modified:
                    detail_lines.append(
                        f"  Before: {json.dumps(modification.get('before', {}))}"
                    )
                    detail_lines.append(
                        f"  After:  {json.dumps(modification.get('after', {}))}"
                    )
                    detail_lines.append("")

    # Build final output with summary first
    lines = [
        "=== DATABASE CHANGES SUMMARY ===",
        f"Total rows added: {total_added}",
        f"Total rows deleted: {total_deleted}",
        f"Total rows modified: {total_modified}",
        f"Tables changed: {', '.join(tables_changed)}",
        "",
    ]
    lines.extend(detail_lines)

    return "\n".join(lines)


def _build_db_diff_judge_prompt(
    criteria: str,
    db_diff_summary: str,
) -> str:
    """Build the user prompt for DB diff evaluation."""
    return f"""Criteria to evaluate: {criteria}

Database Changes:
{db_diff_summary}

Evaluate whether the database changes satisfy the criteria."""


async def db_diff_llm_eval(input: EvalImplInput) -> VerifierResult:
    """
    DB Diff LLM Judge - Evaluate database changes against criteria using LLM.

    This verifier:
    1. Receives DB diff results from the DB_DIFF helper
    2. Formats the diff as a readable summary (all changed tables across all databases)
    3. Calls an LLM judge to evaluate if the changes meet the specified criteria

    Verifier config fields:
    - criteria: The criteria describing expected database changes (required)

    Returns:
    - judge_grade: "pass" or "fail"
    - grade_rationale: Explanation from the LLM
    - db_diff_summary: Summary of database changes evaluated
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    # 1. Get criteria (required)
    criteria = verifier_values.get("criteria", "")
    if not criteria:
        raise ValueError("Missing required field: criteria")

    logger.info(
        f"[DB_DIFF_LLM] task={task_id} | evaluating criteria: {criteria[:100]}..."
    )

    try:
        # 2. Get DB diff from helper results
        # DB_DIFF helper auto-detects database type (SQLite or MySQL/MariaDB dump)
        if not input.helper_results:
            raise ValueError("Missing helper results")

        db_diff_result = input.helper_results.get(HelperIds.DB_DIFF)
        if not db_diff_result:
            logger.warning(
                f"[DB_DIFF_LLM] task={task_id} | no DB diff found, failing criterion"
            )
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                verifier_result_values={
                    "judge_grade": "fail",
                    "grade_rationale": "No database diff was available to evaluate.",
                    "db_diff_summary": "No diff available",
                },
            )

        # 3. Format DB diff for prompt (includes all changed tables)
        db_diff_summary = _format_db_diff_for_prompt(db_diff_result)

        # 4. Get model settings
        model = input.grading_settings.llm_judge_model
        extra_args = input.grading_settings.llm_judge_extra_args

        # 5. Build prompt
        user_prompt = _build_db_diff_judge_prompt(
            criteria=criteria,
            db_diff_summary=db_diff_summary,
        )

        # 6. Build messages
        messages = build_messages(
            system_prompt=DB_DIFF_JUDGE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        logger.debug(
            f"[DB_DIFF_LLM] task={task_id} | prompt:\n"
            f"SYSTEM:\n{DB_DIFF_JUDGE_SYSTEM_PROMPT}\n\n"
            f"USER:\n{user_prompt}"
        )

        # 7. Call LLM with JSON output and retry loop (matches output_llm pattern)
        parsed_response = None
        raw_content = None
        for attempt in range(MAX_JSON_RETRIES):
            response = await call_llm(
                model=model,
                messages=messages,
                timeout=LLM_JUDGE_TIMEOUT,
                extra_args=extra_args,
                response_format={"type": "json_object"},
            )

            choices = response.choices
            if not choices or not isinstance(choices[0], Choices):
                logger.warning(
                    f"[DB_DIFF_LLM] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: empty response"
                )
                continue

            raw_content = choices[0].message.content
            if not raw_content:
                logger.warning(
                    f"[DB_DIFF_LLM] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: empty content"
                )
                continue

            try:
                # Normalize common LLM response quirks before Pydantic validation
                try:
                    raw_json = json.loads(raw_content)
                    # Some LLMs wrap the response in a single-element array
                    if isinstance(raw_json, list) and len(raw_json) == 1:
                        raw_json = raw_json[0]
                        logger.debug(
                            f"[DB_DIFF_LLM] Unwrapped single-element list for task={task_id}"
                        )
                    # Some LLMs return reason as a dict/object instead of string
                    if isinstance(raw_json, dict) and isinstance(
                        raw_json.get("reason"), dict
                    ):
                        raw_json["reason"] = json.dumps(raw_json["reason"])
                        logger.debug(
                            f"[DB_DIFF_LLM] Stringified dict reason for task={task_id}"
                        )
                    raw_content = json.dumps(raw_json)
                except json.JSONDecodeError:
                    pass  # Let model_validate_json handle JSON errors

                parsed_response = DbDiffJudgeResponse.model_validate_json(raw_content)
                break
            except ValidationError as e:
                logger.warning(
                    f"[DB_DIFF_LLM] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
                )
                continue

        if parsed_response is None:
            raise ValueError(f"Invalid JSON after {MAX_JSON_RETRIES} attempts")

        # 8. Build result
        passed = parsed_response.result == 1
        score = 1.0 if passed else 0.0

        logger.info(
            f"[DB_DIFF_LLM] task={task_id} | "
            f"result: {'PASS' if passed else 'FAIL'} | "
            f"criteria: {criteria[:50]}..."
        )

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "judge_grade": "pass" if passed else "fail",
                "grade_rationale": parsed_response.reason,
                "db_diff_summary": db_diff_summary,
            },
        )

    except Exception as e:
        error_msg = f"DB diff LLM evaluation failed: {str(e)}"
        logger.error(f"[DB_DIFF_LLM] task={task_id} | error: {error_msg}")
        raise ValueError(error_msg) from e
