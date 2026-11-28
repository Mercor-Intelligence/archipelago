"""Looker Content Check Verifier - Verify non-database state (Looks, Dashboards, Queries)."""

from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import compare_values


def _compare_content_values(actual: Any, expected: Any) -> bool:
    """Compare content values, with special handling for lists."""
    if actual is None:
        return False

    # List comparison: use sets for unordered matching
    if isinstance(actual, list):
        actual_set = {str(x).lower().strip() for x in actual}
        expected_set = {str(x).lower().strip() for x in str(expected).split(",")}
        return actual_set == expected_set

    # Delegate to standard comparison for other types
    return compare_values(actual, expected)


FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # Look checks
    "Look Count": {"operation": "look_count", "required_params": []},
    "Look Exists": {"operation": "look_exists", "required_params": ["look_id"]},
    "Look Title": {
        "operation": "look_field",
        "field": "title",
        "required_params": ["look_id"],
    },
    "Look Query ID": {
        "operation": "look_field",
        "field": "query_id",
        "required_params": ["look_id"],
    },
    "Look Description": {
        "operation": "look_field",
        "field": "description",
        "required_params": ["look_id"],
    },
    # Dashboard checks
    "Dashboard Count": {"operation": "dashboard_count", "required_params": []},
    "Dashboard Exists": {
        "operation": "dashboard_exists",
        "required_params": ["dashboard_id"],
    },
    "Dashboard Title": {
        "operation": "dashboard_field",
        "field": "title",
        "required_params": ["dashboard_id"],
    },
    "Dashboard Description": {
        "operation": "dashboard_field",
        "field": "description",
        "required_params": ["dashboard_id"],
    },
    "Dashboard Tile Count": {
        "operation": "dashboard_tile_count",
        "required_params": ["dashboard_id"],
    },
    # Query checks
    "Query Count": {"operation": "query_count", "required_params": []},
    "Query Exists": {"operation": "query_exists", "required_params": ["query_id"]},
    "Query Model": {
        "operation": "query_field",
        "field": "model",
        "required_params": ["query_id"],
    },
    "Query View": {
        "operation": "query_field",
        "field": "view",
        "required_params": ["query_id"],
    },
    "Query Fields": {
        "operation": "query_field",
        "field": "fields",
        "required_params": ["query_id"],
    },
}


def _execute_operation(
    looker_state: dict[str, Any],
    field_config: dict[str, Any],
    verifier_values: dict[str, Any],
) -> tuple[Any, str | None]:
    """Execute the configured operation. Returns (result, error_message)."""
    operation = field_config["operation"]

    # Helper to safely get dict from state (JSON could be list if malformed)
    def _get_dict(key: str) -> dict[str, Any]:
        value = looker_state.get(key, {})
        if not isinstance(value, dict):
            return {}
        return value

    # Look operations
    if operation == "look_count":
        looks = _get_dict("looks")
        return len(looks), None

    if operation == "look_exists":
        look_id = str(verifier_values.get("look_id") or "")
        looks = _get_dict("looks")
        return look_id in looks, None

    if operation == "look_field":
        look_id = str(verifier_values.get("look_id") or "")
        field_name = field_config.get("field", "title")
        looks = _get_dict("looks")
        if look_id not in looks:
            return None, f"Look '{look_id}' not found"
        return looks[look_id].get(field_name), None

    # Dashboard operations
    if operation == "dashboard_count":
        dashboards = _get_dict("dashboards")
        return len(dashboards), None

    if operation == "dashboard_exists":
        dashboard_id = str(verifier_values.get("dashboard_id") or "")
        dashboards = _get_dict("dashboards")
        return dashboard_id in dashboards, None

    if operation == "dashboard_field":
        dashboard_id = str(verifier_values.get("dashboard_id") or "")
        field_name = field_config.get("field", "title")
        dashboards = _get_dict("dashboards")
        if dashboard_id not in dashboards:
            return None, f"Dashboard '{dashboard_id}' not found"
        return dashboards[dashboard_id].get(field_name), None

    if operation == "dashboard_tile_count":
        dashboard_id = str(verifier_values.get("dashboard_id") or "")
        tiles = _get_dict("tiles")
        if dashboard_id not in tiles:
            return 0, None
        tile_list = tiles[dashboard_id]
        return len(tile_list) if isinstance(tile_list, list) else 0, None

    # Query operations
    if operation == "query_count":
        queries = _get_dict("queries")
        return len(queries), None

    if operation == "query_exists":
        query_id = str(verifier_values.get("query_id") or "")
        queries = _get_dict("queries")
        return query_id in queries, None

    if operation == "query_field":
        query_id = str(verifier_values.get("query_id") or "")
        field_name = field_config.get("field", "model")
        queries = _get_dict("queries")
        if query_id not in queries:
            return None, f"Query '{query_id}' not found"
        return queries[query_id].get(field_name), None

    return None, f"Unknown operation: {operation}"


async def looker_content_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify Looker content state (Looks, Dashboards, Queries) from snapshot."""
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    if not input.helper_results:
        raise ValueError("Helper results not available")

    field_to_check = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check:
        raise ValueError("field_to_check is required")

    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        raise ValueError("expected_value is required")

    # Strip arrow hint if present for lookup
    field_key = (
        field_to_check.split(" → ")[0] if " → " in field_to_check else field_to_check
    )
    field_config = FIELD_MAPPINGS.get(field_key)
    if not field_config:
        raise ValueError(f"Unknown field_to_check: {field_to_check}")

    # Validate required params
    for param in field_config.get("required_params", []):
        if input.verifier.verifier_values.get(param) is None:
            raise ValueError(f"Required parameter '{param}' is missing")

    looker_state = input.helper_results.get(HelperIds.LOOKER_STATE)
    if not looker_state:
        raise ValueError("Looker state not available from helper")

    try:
        result, error = _execute_operation(
            looker_state, field_config, input.verifier.verifier_values
        )
    except Exception as e:
        logger.error(f"Error executing content check: {e}")
        raise ValueError(f"Content check failed: {str(e)}") from e

    if error:
        raise ValueError(f"Operation failed: {error}")

    logger.info(
        f"Looker content check: {field_to_check} = {result} (expected: {expected_value})"
    )

    matches = _compare_content_values(result, expected_value)
    score = 1.0 if matches else 0.0

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "field_to_check": field_to_check,
            "expected_value": expected_value,
            "actual_value": result,
            "matches": matches,
        },
        message=f"Content check {'passed' if matches else 'failed'}: {field_to_check} = {result}",
    )
