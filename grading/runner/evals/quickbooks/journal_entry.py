"""QuickBooks Journal Entry Verifier."""

import json
from decimal import Decimal, InvalidOperation

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import find_journal_entry_fuzzy, verify_je_lines


async def quickbooks_journal_entry_eval(input: EvalImplInput) -> VerifierResult:
    """Verify specific journal entry with DR/CR amounts."""
    # Input validation
    je_description = input.verifier.verifier_values.get("je_description")
    if not je_description:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"found": False},
            message="je_description is required",
        )

    # Parse JSON strings if needed (registry stores as TEXT with JSON)
    expected_debits_raw = input.verifier.verifier_values.get("expected_debits", [])
    expected_credits_raw = input.verifier.verifier_values.get("expected_credits", [])

    # Handle both JSON strings and direct lists
    if isinstance(expected_debits_raw, str):
        try:
            expected_debits = (
                json.loads(expected_debits_raw) if expected_debits_raw else []
            )
        except (json.JSONDecodeError, TypeError):
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={"found": False},
                message=f"Invalid JSON in expected_debits: {expected_debits_raw}",
            )
    else:
        expected_debits = expected_debits_raw or []

    if isinstance(expected_credits_raw, str):
        try:
            expected_credits = (
                json.loads(expected_credits_raw) if expected_credits_raw else []
            )
        except (json.JSONDecodeError, TypeError):
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={"found": False},
                message=f"Invalid JSON in expected_credits: {expected_credits_raw}",
            )
    else:
        expected_credits = expected_credits_raw or []

    # Validate that at least one of debits or credits is specified
    if not expected_debits and not expected_credits:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "found": False,
                "debits_specified": False,
                "credits_specified": False,
            },
            message="At least one of expected_debits or expected_credits must be specified",
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
    journal_entries = qb_data.get("journal_entries", [])

    # Find JE by description (fuzzy match)
    matching_je = find_journal_entry_fuzzy(journal_entries, je_description)

    if not matching_je:
        available_entries = [
            je.get("doc_number") or je.get("description", "") for je in journal_entries
        ][:10]
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "found": False,
                "searched_for": je_description,
                "available_entries": available_entries,
            },
            message=f"Journal entry '{je_description}' not found",
        )

    # Verify debits and credits
    debits_correct = (
        verify_je_lines(matching_je["debits"], expected_debits, tolerance)
        if expected_debits
        else True
    )
    credits_correct = (
        verify_je_lines(matching_je["credits"], expected_credits, tolerance)
        if expected_credits
        else True
    )

    all_correct = debits_correct and credits_correct

    result = VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if all_correct else 0.0,
        verifier_result_values={
            "found": True,
            "je_id": matching_je["id"],
            "debits_match": debits_correct,
            "credits_match": credits_correct,
            "debits_count": len(matching_je.get("debits", [])),
            "credits_count": len(matching_je.get("credits", [])),
        },
        message=f"JE '{je_description}': {'✓ Correct' if all_correct else '✗ Incorrect'}",
    )

    # Log success
    if all_correct:
        logger.info(
            f"✓ Verifier {input.verifier.verifier_id} passed: JE '{je_description}' matched"
        )
    else:
        logger.warning(
            f"✗ Verifier {input.verifier.verifier_id} failed: JE '{je_description}' DR/CR mismatch"
        )

    return result
