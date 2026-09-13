"""Tableau Field Check Verifier - Check field values in Tableau entities."""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import (
    count_by_site_role,
    filter_by_pattern,
    filter_by_site,
    find_by_id,
    find_by_name,
    get_field_value,
)

FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # Workbook checks
    "Workbook Count": {"data_source": "workbooks", "operation": "count"},
    "Workbook Count by Site": {
        "data_source": "workbooks",
        "operation": "count_by_site",
        "required_params": ["site_id"],
    },
    "Workbook Exists": {
        "data_source": "workbooks",
        "operation": "exists_by_name",
        "required_params": ["workbook_name"],
    },
    "Workbook Exists on Site": {
        "data_source": "workbooks",
        "operation": "exists_on_site",
        "required_params": ["workbook_name", "site_id"],
    },
    "Workbook Field": {
        "data_source": "workbooks",
        "operation": "get_field_by_name",
        "required_params": ["workbook_name", "field_name"],
    },
    # User checks
    "User Count": {"data_source": "users", "operation": "count"},
    "User Count by Site": {
        "data_source": "users",
        "operation": "count_by_site",
        "required_params": ["site_id"],
    },
    "User Count by Role": {
        "data_source": "users",
        "operation": "count_by_role",
        "required_params": ["site_role"],
    },
    "User Count by Pattern": {
        "data_source": "users",
        "operation": "count_by_pattern",
        "required_params": ["name_pattern"],
    },
    "User Exists": {
        "data_source": "users",
        "operation": "exists_by_name",
        "required_params": ["user_name"],
    },
    "User Exists on Site": {
        "data_source": "users",
        "operation": "exists_on_site",
        "required_params": ["user_name", "site_id"],
    },
    "User Site Role": {
        "data_source": "users",
        "operation": "get_user_role",
        "required_params": ["user_name"],
    },
    "User Field": {
        "data_source": "users",
        "operation": "get_field_by_name",
        "required_params": ["user_name", "field_name"],
    },
    # Site checks
    "Site Count": {"data_source": "sites", "operation": "count"},
    "Site Exists": {
        "data_source": "sites",
        "operation": "exists_by_name",
        "required_params": ["site_name"],
    },
    "Site Exists by ID": {
        "data_source": "sites",
        "operation": "exists_by_id",
        "required_params": ["site_id"],
    },
    # Project checks
    "Project Count": {"data_source": "projects", "operation": "count"},
    "Project Count by Site": {
        "data_source": "projects",
        "operation": "count_by_site",
        "required_params": ["site_id"],
    },
    "Project Exists": {
        "data_source": "projects",
        "operation": "exists_by_name",
        "required_params": ["project_name"],
    },
    "Project Exists on Site": {
        "data_source": "projects",
        "operation": "exists_on_site",
        "required_params": ["project_name", "site_id"],
    },
    "Project Field": {
        "data_source": "projects",
        "operation": "get_field_by_name",
        "required_params": ["project_name", "field_name"],
    },
    "Project Owner": {
        "data_source": "projects",
        "operation": "get_project_owner",
        "required_params": ["project_name"],
    },
    "Project Owner Name": {
        "data_source": "projects",
        "operation": "get_project_owner_name",
        "required_params": ["project_name"],
        "lookup_data_source": "users",
    },
    "Workbook Owner": {
        "data_source": "workbooks",
        "operation": "get_workbook_owner",
        "required_params": ["workbook_name"],
    },
    "Workbook Owner Name": {
        "data_source": "workbooks",
        "operation": "get_workbook_owner_name",
        "required_params": ["workbook_name"],
        "lookup_data_source": "users",
    },
    # Datasource checks
    "Datasource Count": {"data_source": "datasources", "operation": "count"},
    "Datasource Exists": {
        "data_source": "datasources",
        "operation": "exists_by_name",
        "required_params": ["datasource_name"],
    },
    # View checks
    "View Count": {"data_source": "views", "operation": "count"},
    "View Exists": {
        "data_source": "views",
        "operation": "exists_by_name",
        "required_params": ["view_name"],
    },
    # Group checks
    "Group Count": {"data_source": "groups", "operation": "count"},
    "Group Exists": {
        "data_source": "groups",
        "operation": "exists_by_name",
        "required_params": ["group_name"],
    },
}


def _execute_operation(
    data: list[dict[str, Any]],
    field_config: dict[str, Any],
    verifier_values: dict[str, Any],
    tableau_state: dict[str, Any] | None = None,
) -> Any:
    """Execute the configured operation on the data."""
    operation = field_config["operation"]

    # Count operations
    if operation == "count":
        return len(data)
    if operation == "count_by_site":
        site_id = str(verifier_values.get("site_id") or "")
        return len(filter_by_site(data, site_id))
    if operation == "count_by_role":
        site_role = str(verifier_values.get("site_role") or "")
        return count_by_site_role(data, site_role)
    if operation == "count_by_pattern":
        pattern = str(verifier_values.get("name_pattern") or "")
        return len(filter_by_pattern(data, "name", pattern))

    # Existence checks
    if operation == "exists_by_name":
        name_param = field_config.get("required_params", ["name"])[0]
        name = str(verifier_values.get(name_param) or "")
        return find_by_name(data, name) is not None
    if operation == "exists_by_id":
        id_param = field_config.get("required_params", ["id"])[0]
        id_value = str(verifier_values.get(id_param) or "")
        return find_by_id(data, id_value) is not None
    if operation == "exists_on_site":
        name_param = field_config.get("required_params", ["name"])[0]
        name = str(verifier_values.get(name_param) or "")
        site_id = str(verifier_values.get("site_id") or "")
        item = find_by_name(data, name)
        if item is None:
            return False
        return str(item.get("site_id", "")) == site_id

    # Field value retrieval
    if operation == "get_field_by_name":
        name_param = field_config.get("required_params", ["name"])[0]
        name = str(verifier_values.get(name_param) or "")
        field_name = str(verifier_values.get("field_name") or "")
        item = find_by_name(data, name)
        return get_field_value(item, field_name)
    if operation == "get_user_role":
        user_name = str(verifier_values.get("user_name") or "")
        user = find_by_name(data, user_name)
        return user.get("site_role") if user else None

    if operation == "get_project_owner":
        # Returns the owner_id (useful if you want to compare against a known ID)
        project_name = str(verifier_values.get("project_name") or "")
        project = find_by_name(data, project_name)
        if project is None:
            return None
        return project.get("owner_id")

    if operation == "get_project_owner_name":
        # Returns the owner's name by resolving owner_id to user name
        project_name = str(verifier_values.get("project_name") or "")
        project = find_by_name(data, project_name)
        if project is None:
            return None
        owner_id = project.get("owner_id")
        if owner_id is None:
            return None
        # Look up user name from users table
        if tableau_state is None:
            return owner_id  # Fallback to ID if no state available
        users = tableau_state.get("users", [])
        owner = find_by_id(users, str(owner_id))
        return owner.get("name") if owner else None

    if operation == "get_workbook_owner":
        # Returns the owner_id
        workbook_name = str(verifier_values.get("workbook_name") or "")
        workbook = find_by_name(data, workbook_name)
        if workbook is None:
            return None
        return workbook.get("owner_id")

    if operation == "get_workbook_owner_name":
        # Returns the owner's name by resolving owner_id to user name
        workbook_name = str(verifier_values.get("workbook_name") or "")
        workbook = find_by_name(data, workbook_name)
        if workbook is None:
            return None
        owner_id = workbook.get("owner_id")
        if owner_id is None:
            return None
        # Look up user name from users table
        if tableau_state is None:
            return owner_id  # Fallback to ID if no state available
        users = tableau_state.get("users", [])
        owner = find_by_id(users, str(owner_id))
        return owner.get("name") if owner else None

    raise ValueError(f"Unknown operation: {operation}")


def _compare_values(actual: Any, expected: Any, tolerance: Decimal | None) -> bool:
    """Compare actual vs expected with optional tolerance for numbers."""
    if actual is None:
        return False

    # Boolean comparison
    if isinstance(actual, bool):
        expected_bool = str(expected).lower() in ("true", "1", "yes")
        return actual == expected_bool

    # Try numeric comparison
    try:
        actual_num = Decimal(str(actual))
        expected_num = Decimal(str(expected))
        if tolerance:
            return abs(actual_num - expected_num) <= tolerance
        return actual_num == expected_num
    except (ValueError, TypeError, InvalidOperation):
        pass

    # String comparison (case-insensitive)
    return str(actual).lower() == str(expected).lower()


async def tableau_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify Tableau field values using dropdown + value interface."""
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Validate helper results available
    if not input.helper_results:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    # Get field selection
    field_to_check = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="field_to_check is required",
        )

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    # Get field configuration (strip arrow hint if present for lookup)
    field_key = (
        field_to_check.split(" → ")[0] if " → " in field_to_check else field_to_check
    )
    field_config = FIELD_MAPPINGS.get(field_key)
    if not field_config:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Unknown field_to_check: {field_to_check}",
        )

    # Validate required params
    for param in field_config.get("required_params", []):
        if input.verifier.verifier_values.get(param) is None:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Required parameter '{param}' is missing",
            )

    # Get Tableau state from helper
    tableau_state = input.helper_results.get(HelperIds.TABLEAU_STATE)
    if not tableau_state:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Tableau state not available from helper",
        )

    # Get the data source
    data_source = field_config["data_source"]
    data = tableau_state.get(data_source, [])

    try:
        result = _execute_operation(
            data, field_config, input.verifier.verifier_values, tableau_state
        )
        logger.info(
            f"Tableau field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing field check: {e}")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Field check failed: {str(e)}",
        )

    # Get tolerance for numeric comparison
    tolerance = None
    tolerance_str = input.verifier.verifier_values.get("tolerance")
    if tolerance_str:
        try:
            tolerance = Decimal(str(tolerance_str))
        except (ValueError, TypeError, InvalidOperation):
            pass

    matches = _compare_values(result, expected_value, tolerance)
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
            "tolerance": str(tolerance) if tolerance else None,
            "matches": matches,
        },
        message=f"Field check {'passed' if matches else 'failed'}: {field_to_check} = {result}",
    )
