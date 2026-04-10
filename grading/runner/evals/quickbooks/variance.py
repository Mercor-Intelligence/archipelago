"""QuickBooks Variance Verifier."""

import zipfile
from decimal import Decimal, InvalidOperation

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.evals.output_llm.utils.snapshot_utils import read_file_from_snapshot_zip
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import find_line_item_fuzzy, parse_budget_csv_from_artifacts


async def quickbooks_variance_eval(input: EvalImplInput) -> VerifierResult:
    """Verify variance calculation against budget."""
    # Input validation
    line_item = input.verifier.verifier_values.get("line_item_name")
    if not line_item:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message="line_item_name is required",
        )

    budget_artifact_id = input.verifier.verifier_values.get("budget_artifact_id")
    if not budget_artifact_id:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message="budget_artifact_id is required",
        )

    expected_variance_pct_raw = input.verifier.verifier_values.get(
        "expected_variance_pct"
    )
    if expected_variance_pct_raw is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message="expected_variance_pct is required",
        )

    try:
        expected_variance_pct = Decimal(str(expected_variance_pct_raw))
    except (ValueError, TypeError, InvalidOperation) as e:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message=f"Invalid expected_variance_pct: {expected_variance_pct_raw} ({e})",
        )

    # Validate helper results are available
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message="Helper results not available",
        )

    # Get actual P&L value (with fuzzy matching)
    qb_data = input.helper_results[HelperIds.QUICKBOOKS_STATE]
    pnl_report = qb_data.get("pnl_report", {})
    actual_value = find_line_item_fuzzy(pnl_report, line_item)

    if actual_value is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "found": False,
                "searched_for": line_item,
                "available_items": list(pnl_report.keys())[:10],
            },
            message=f"Line item '{line_item}' not found in P&L report",
        )

    # Fetch budget artifact from initial snapshot
    try:
        input.initial_snapshot_bytes.seek(0)
        with zipfile.ZipFile(input.initial_snapshot_bytes, "r") as snapshot_zip:
            budget_bytes = read_file_from_snapshot_zip(snapshot_zip, budget_artifact_id)
            if not budget_bytes:
                return VerifierResult(
                    verifier_id=input.verifier.verifier_id,
                    verifier_version=input.verifier.verifier_version,
                    score=0.0,
                    status=VerifierResultStatus.ERROR,
                    verifier_result_values={
                        "found": False,
                        "budget_artifact_id": budget_artifact_id,
                    },
                    message=f"Budget artifact '{budget_artifact_id}' not found in snapshot",
                )

        # Reset BytesIO position after use for potential reuse
        input.initial_snapshot_bytes.seek(0)
    except Exception as e:
        logger.error(f"Error reading budget artifact from snapshot: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message=f"Failed to read budget artifact: {str(e)}",
        )

    # Parse budget CSV
    budget_data = await parse_budget_csv_from_artifacts(
        budget_artifact_id, {budget_artifact_id: budget_bytes}
    )
    budget_value = find_line_item_fuzzy(budget_data, line_item)

    if budget_value is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "found": False,
                "budget_artifact_id": budget_artifact_id,
                "available_budget_items": list(budget_data.keys())[:10],
            },
            message=f"Budget value for '{line_item}' not found in reference artifact",
        )

    # Check for zero budget (variance percentage is undefined)
    if budget_value == 0:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "found": True,
                "budget_value": 0.0,
                "actual_value": float(actual_value),
            },
            message=f"Cannot calculate variance percentage for '{line_item}': budget value is zero (division by zero undefined)",
        )

    # Calculate variance
    variance_dollar = actual_value - budget_value
    calculated_variance_pct = variance_dollar / budget_value * 100

    # Check if agent's calculation matches (0.1% tolerance)
    variance_diff = abs(calculated_variance_pct - expected_variance_pct)
    is_correct = variance_diff <= Decimal("0.1")

    result = VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if is_correct else 0.0,
        verifier_result_values={
            "found": True,
            "actual_value": float(actual_value),
            "budget_value": float(budget_value),
            "variance_dollar": float(variance_dollar),
            "calculated_variance_pct": float(calculated_variance_pct),
            "agent_variance_pct": float(expected_variance_pct),
            "variance_diff": float(variance_diff),
            "is_correct": is_correct,
        },
        message=f"{line_item} variance: {calculated_variance_pct:.2f}% (agent: {expected_variance_pct:.2f}%)",
    )

    # Log success
    if is_correct:
        logger.info(
            f"✓ Verifier {input.verifier.verifier_id} passed: {line_item} variance calculation correct"
        )
    else:
        logger.warning(
            f"✗ Verifier {input.verifier.verifier_id} failed: {line_item} variance diff {variance_diff:.2f}% exceeds tolerance"
        )

    return result
