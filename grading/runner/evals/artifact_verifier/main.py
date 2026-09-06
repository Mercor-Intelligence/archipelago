"""Artifact verifier eval - evaluates assertions against parsed artifact state.

This eval runs typed assertions (row_exists, field_equals, row_count, etc.)
against ArtifactState tables that have been extracted from artifact files
using config-driven parsing.

Data flow:
1. User selects a check to run via verifier_values["check_to_run"]
2. Check definition is retrieved from eval_config_values["checks"][check_name]
3. ArtifactState is retrieved from helper_results[ARTIFACT_STATE]
4. Params are extracted from verifier_values (artifact__{check_name}__{param_name})
5. Param placeholders (:param_name) in assertions are substituted with actual values
6. Assertions are evaluated against the artifact tables
7. Score is calculated as (passed_count / total_count)
"""

from typing import Any

from runner.evals.artifact_verifier.assertions import (
    AssertionResult,
    evaluate_assertion,
)
from runner.evals.models import EvalImplInput
from runner.helpers.artifact_state.models import ArtifactState
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus


def _error_result(
    verifier_id: str,
    verifier_version: int,
    message: str,
) -> VerifierResult:
    """Return an error result with score 0.0."""
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=0.0,
        verifier_result_values={"error": message},
        status=VerifierResultStatus.ERROR,
        message=message,
    )


def _extract_check_params(
    verifier_values: dict[str, Any],
    check_name: str,
) -> dict[str, Any]:
    """Extract parameters for a specific check from verifier_values.

    Field IDs follow the pattern artifact__{check_name}__{param_name}.
    """
    prefix = f"artifact__{check_name}__"
    return {
        field_id[len(prefix) :]: value
        for field_id, value in verifier_values.items()
        if field_id.startswith(prefix)
    }


def _substitute_value(value: Any, params: dict[str, Any]) -> Any:
    """Substitute :param_name placeholders in a single value."""
    if isinstance(value, str) and value.startswith(":"):
        param_name = value[1:]
        return params.get(param_name, value)
    if isinstance(value, dict):
        return {k: _substitute_value(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_value(item, params) for item in value]
    return value


def _substitute_params(
    assertions: list[dict[str, Any]], params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Substitute :param_name placeholders in assertion values with actual params."""
    if not params:
        return assertions
    return [
        {k: _substitute_value(v, params) for k, v in assertion.items()}
        for assertion in assertions
    ]


async def artifact_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Execute artifact verification checks against ArtifactState.

    Reads check definitions from eval_config.eval_config_values["checks"],
    gets the selected check from verifier.verifier_values["check_to_run"],
    substitutes any :param_name placeholders using params stored in verifier_values
    (keyed as artifact__{check_name}__{param_name}), and evaluates all assertions.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    config_values = input.eval_config.eval_config_values
    verifier_values = input.verifier.verifier_values

    # 1. Get check name (selected by user in verifier config)
    check_name = verifier_values.get("check_to_run")
    if not check_name:
        return _error_result(
            verifier_id,
            verifier_version,
            "No check selected. Set 'check_to_run' field.",
        )

    # 2. Get check definition from snapshotted config
    checks: dict[str, Any] = config_values.get("checks", {})
    check_def = checks.get(check_name)
    if check_def is None:
        available = list(checks.keys())
        return _error_result(
            verifier_id,
            verifier_version,
            f"Unknown check: {check_name}. Available: {available}",
        )

    # 3. Get ArtifactState from helper results
    if not input.helper_results:
        return _error_result(
            verifier_id,
            verifier_version,
            "Missing helper results (ARTIFACT_STATE required)",
        )

    state_data = input.helper_results.get(HelperIds.ARTIFACT_STATE)
    if state_data is None:
        return _error_result(
            verifier_id,
            verifier_version,
            "artifact_state helper not found in results",
        )

    # Convert dict to ArtifactState if needed (helper returns dict from model_dump)
    if isinstance(state_data, dict):
        state = ArtifactState(**state_data)
    elif isinstance(state_data, ArtifactState):
        state = state_data
    else:
        return _error_result(
            verifier_id,
            verifier_version,
            f"Invalid artifact_state type: {type(state_data).__name__}",
        )

    # 4. Check for parse errors
    if state.parse_errors:
        return _error_result(
            verifier_id,
            verifier_version,
            f"Parse errors: {state.parse_errors}",
        )

    # 5. Extract params and substitute into assertions
    assertions_data: list[dict[str, Any]] = check_def.get("assertions", [])
    if not assertions_data:
        # Vacuous truth - no assertions means pass
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=1.0,
            verifier_result_values={
                "check_name": check_name,
                "passed": True,
                "passed_count": 0,
                "total_count": 0,
                "results": [],
            },
            status=VerifierResultStatus.OK,
        )

    params = _extract_check_params(verifier_values, check_name)
    assertions_data = _substitute_params(assertions_data, params)

    results: list[AssertionResult] = []
    for assertion_data in assertions_data:
        result = evaluate_assertion(state, assertion_data)
        results.append(result)

    passed_count = sum(1 for r in results if r.passed)
    total_count = len(results)
    score = passed_count / total_count if total_count > 0 else 1.0
    all_passed = passed_count == total_count

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=score,
        verifier_result_values={
            "check_name": check_name,
            "passed": all_passed,
            "passed_count": passed_count,
            "total_count": total_count,
            "results": [r.model_dump() for r in results],
        },
        status=VerifierResultStatus.OK,
        message="" if all_passed else f"{passed_count}/{total_count} assertions passed",
    )
