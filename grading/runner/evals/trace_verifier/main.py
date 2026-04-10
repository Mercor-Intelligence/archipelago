"""Trace verifier eval for analyzing Playwright traces.

This verifier evaluates agent behavior by analyzing Playwright trace data:
- Milestones: Verify that expected workflow steps occurred in order
- Behaviors: Detect anti-patterns like retry loops or error cascades

Configuration comes from [arco.verifiers.trace] in arco.toml.
"""

from typing import Any

from runner.evals.models import EvalImplInput
from runner.evals.trace_verifier.behaviors import evaluate_behavior
from runner.evals.trace_verifier.milestones import evaluate_milestone
from runner.helpers.models import HelperIds
from runner.helpers.trace_models import PlaywrightTraceData
from runner.models import VerifierResult, VerifierResultStatus


def _config_error(
    verifier_id: str, verifier_version: int, message: str
) -> VerifierResult:
    """Return a result for configuration errors (status=ERROR, score=0.0)."""
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=0.0,
        verifier_result_values={"error": message},
        status=VerifierResultStatus.ERROR,
        message=message,
    )


def _find_check_by_name(checks: list[Any], check_name: str) -> dict[str, Any] | None:
    """Find a check definition by name in a list of checks.

    Args:
        checks: List of check definitions (dicts or Pydantic models)
        check_name: Name to search for

    Returns:
        Check definition dict or None if not found
    """
    for check in checks:
        check_dict = check if isinstance(check, dict) else check.model_dump()
        if check_dict.get("name") == check_name:
            return check_dict
    return None


async def trace_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Trace verifier that analyzes Playwright traces.

    Reads check_type and check_to_run from verifier_values to determine
    which check to run. check_type can be "milestone" or "behavior".

    Args:
        input: EvalImplInput with verifier config and helper results

    Returns:
        VerifierResult with score and check details
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    config_values = input.eval_config.eval_config_values
    verifier_values = input.verifier.verifier_values

    # 1. Determine check type and name
    check_type = verifier_values.get("check_type")
    check_name = verifier_values.get("check_to_run")

    if not check_type:
        return _config_error(
            verifier_id, verifier_version, "Missing check_type in verifier_values"
        )
    if not check_name:
        return _config_error(
            verifier_id, verifier_version, "Missing check_to_run in verifier_values"
        )

    # 2. Get trace data from helper
    if not input.helper_results:
        return _config_error(
            verifier_id,
            verifier_version,
            "Missing helper results (PLAYWRIGHT_TRACE_PARSER required)",
        )

    trace_data_dict = input.helper_results.get(HelperIds.PLAYWRIGHT_TRACE_PARSER)
    if trace_data_dict is None:
        return _config_error(
            verifier_id,
            verifier_version,
            "PLAYWRIGHT_TRACE_PARSER helper result not found",
        )

    # Convert dict back to PlaywrightTraceData model
    trace_data = PlaywrightTraceData.model_validate(trace_data_dict)

    # 3. Dispatch based on check type
    if check_type == "milestone":
        milestones = config_values.get("milestones", [])
        check_def = _find_check_by_name(milestones, check_name)
        if not check_def:
            return _config_error(
                verifier_id,
                verifier_version,
                f"Unknown milestone check: {check_name}",
            )
        result = evaluate_milestone(trace_data, check_def)

    elif check_type == "behavior":
        behaviors = config_values.get("behaviors", [])
        check_def = _find_check_by_name(behaviors, check_name)
        if not check_def:
            return _config_error(
                verifier_id,
                verifier_version,
                f"Unknown behavior check: {check_name}",
            )
        result = evaluate_behavior(trace_data, check_def)

    else:
        return _config_error(
            verifier_id,
            verifier_version,
            f"Unknown check_type: {check_type}. Must be 'milestone' or 'behavior'",
        )

    # Build final VerifierResult (use ERROR status for config/eval errors)
    result_values = result["verifier_result_values"]
    if result_values.get("error"):
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=result["score"],
            verifier_result_values=result_values,
            status=VerifierResultStatus.ERROR,
            message=result_values.get("error", "Evaluation error"),
        )

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=result["score"],
        verifier_result_values=result_values,
        status=VerifierResultStatus.OK,
        message="" if result["score"] == 1.0 else "Check did not fully pass",
    )
