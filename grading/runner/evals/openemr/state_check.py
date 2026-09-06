"""OpenEMR State Check - SQL-only database state verification.

This verifier checks clinical encounter database state without LLM evaluation.
Use this for simple state verification. For quality evaluation, use OUTPUT_LLM separately.
"""

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus

from .clinical_verification import (
    _extract_data_from_snapshot,
)


async def openemr_state_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify clinical encounter database state using SQL checks only.

    This verifier is self-contained and extracts data directly from the snapshot.
    No external helper is required.

    Verifies:
    - Notes were added to the encounter
    - Orders were placed
    - Differential diagnosis was documented (if enabled)
    - Medications were prescribed
    - Lab results were reviewed

    Args:
        input: Framework-provided evaluation input with snapshots

    Returns:
        VerifierResult with score and detailed result values
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    try:
        # Extract config from UI (verifier_values is the correct attribute)
        config_dict = input.verifier.verifier_values
        encounter_id_filter = config_dict.get("encounter_id")  # Optional filter
        check_differential = config_dict.get("check_differential", True)

        # Extract data directly from snapshot (no helper needed)
        encounter_data = _extract_data_from_snapshot(
            input.final_snapshot_bytes,
            encounter_id=encounter_id_filter,
        )

        # Check for extraction errors
        if encounter_data.get("error"):
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                message=f"Data extraction error: {encounter_data['error']}",
                verifier_result_values=encounter_data,
            )

        if not encounter_data or not encounter_data.get("encounter"):
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                message="No encounter data found. Check snapshot contains OpenEMR database.",
                verifier_result_values={"error": "Missing or empty encounter data"},
            )

        encounter_id = encounter_data.get("encounter_id")

        # Get SQL checks from extraction
        sql_checks_raw = encounter_data.get("sql_checks") or {}
        sql_feedback = encounter_data.get("sql_feedback") or []

        sql_checks = {
            name: check.get("passed", False) if isinstance(check, dict) else check
            for name, check in sql_checks_raw.items()
        }

        # Calculate SQL score (percentage of checks passed)
        sql_score = (
            sum(1 for passed in sql_checks.values() if passed) / len(sql_checks) * 100
            if sql_checks
            else 0.0
        )

        # Determine pass/fail
        critical_sql_checks = ["notes_added"]
        if check_differential:
            critical_sql_checks.append("differential_documented")

        critical_passed = all(sql_checks.get(c, False) for c in critical_sql_checks)
        overall_pass = critical_passed and sql_score >= 60

        # Build feedback
        feedback_text = "\n".join(sql_feedback) if sql_feedback else "All checks passed"

        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=sql_score / 100.0,
            status=VerifierResultStatus.OK,
            message=f"State check: {sql_score:.1f}/100 {'PASS' if overall_pass else 'FAIL'}",
            verifier_result_values={
                "encounter_id": encounter_id,
                "passed": overall_pass,
                "score": sql_score,
                "sql_checks": sql_checks,
                "feedback": feedback_text,
                "critical_checks_passed": critical_passed,
            },
        )

    except Exception as e:
        logger.exception("State check failed")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            message=f"Verification error: {e!s}",
            verifier_result_values={"error": str(e)},
        )
