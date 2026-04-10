"""QuickBooks Report Line Item Verifier."""

from decimal import Decimal, InvalidOperation

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import find_line_item_fuzzy


async def quickbooks_report_line_item_eval(input: EvalImplInput) -> VerifierResult:
    """Verify line item from QB financial report (P&L, Balance Sheet)."""
    # Input validation
    report_type = input.verifier.verifier_values.get("report_type")
    if report_type not in ["pnl_report", "balance_sheet"]:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "found": False,
                "report_type": report_type,
            },
            message=f"Invalid report_type: '{report_type}'. Must be 'pnl_report' or 'balance_sheet'.",
        )

    line_item_name = input.verifier.verifier_values.get("line_item_name")
    if not line_item_name:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message="line_item_name is required",
        )

    expected_value_raw = input.verifier.verifier_values.get("expected_value")
    if expected_value_raw is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message="expected_value is required",
        )

    try:
        expected_value = Decimal(str(expected_value_raw))
    except (ValueError, TypeError, InvalidOperation) as e:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message=f"Invalid expected_value: {expected_value_raw} ({e})",
        )

    # Get tolerance with validation
    try:
        tolerance_value = input.verifier.verifier_values.get("tolerance", 0.01)
        if tolerance_value is None:
            tolerance_value = 0.01
        tolerance = Decimal(str(tolerance_value))
    except (ValueError, TypeError, InvalidOperation) as e:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message=f"Invalid tolerance value: {str(e)}",
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

    qb_data = input.helper_results[HelperIds.QUICKBOOKS_STATE]
    report_data = qb_data.get(report_type, {})

    # Fuzzy match line item
    actual_value = find_line_item_fuzzy(report_data, line_item_name)

    if actual_value is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "found": False,
                "searched_for": line_item_name,
                "available_items": list(report_data.keys())[:10],
            },
            message=f"Line item '{line_item_name}' not found in {report_type}",
        )

    # Compare values - convert actual to Decimal if needed
    actual_decimal = Decimal(str(actual_value))
    variance = abs(actual_decimal - expected_value)
    within_tolerance = variance <= tolerance

    result = VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if within_tolerance else 0.0,
        verifier_result_values={
            "found": True,
            "actual_value": float(actual_value),
            "expected_value": float(expected_value),
            "variance": float(variance),
            "within_tolerance": within_tolerance,
        },
        message=f"{line_item_name}: ${actual_value:,.2f} (expected ${expected_value:,.2f})",
    )

    # Log success
    if within_tolerance:
        logger.info(
            f"✓ Verifier {input.verifier.verifier_id} passed: {line_item_name} matched within tolerance"
        )
    else:
        logger.warning(
            f"✗ Verifier {input.verifier.verifier_id} failed: {line_item_name} variance ${variance} exceeds tolerance ${tolerance}"
        )

    return result
