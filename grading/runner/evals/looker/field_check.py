"""Looker Field Check Verifier - Verify SQL query results against expected values."""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import (
    compare_values,
    execute_sql,
    get_aggregate,
    get_field_value,
    get_row_count,
)

FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # SQL query checks
    "SQL Query Result": {"operation": "sql_query", "required_params": ["sql"]},
    "SQL Query Result (Rounded)": {
        "operation": "sql_query_rounded",
        "required_params": ["sql"],  # decimal_places has default of 2
    },
    # Row count checks
    "Row Count": {"operation": "row_count", "required_params": ["table_name"]},
    "Row Count with Condition": {
        "operation": "row_count_condition",
        "required_params": ["table_name", "condition"],
    },
    # Aggregate checks
    "Average": {
        "operation": "aggregate",
        "agg_func": "AVG",
        "required_params": ["table_name", "field"],
    },
    "Sum": {
        "operation": "aggregate",
        "agg_func": "SUM",
        "required_params": ["table_name", "field"],
    },
    "Min": {
        "operation": "aggregate",
        "agg_func": "MIN",
        "required_params": ["table_name", "field"],
    },
    "Max": {
        "operation": "aggregate",
        "agg_func": "MAX",
        "required_params": ["table_name", "field"],
    },
    "Average with Condition": {
        "operation": "aggregate_condition",
        "agg_func": "AVG",
        "required_params": ["table_name", "field", "condition"],
    },
    "Sum with Condition": {
        "operation": "aggregate_condition",
        "agg_func": "SUM",
        "required_params": ["table_name", "field", "condition"],
    },
    # Field value checks
    "Field Value": {
        "operation": "field_value",
        "required_params": ["table_name", "field", "key_field", "key_value"],
    },
    # Table metadata checks
    "Table Exists": {"operation": "table_exists", "required_params": ["table_name"]},
    "Table Row Count": {
        "operation": "table_row_count",
        "required_params": ["table_name"],
    },
}


def _execute_operation(
    looker_state: dict[str, Any],
    field_config: dict[str, Any],
    verifier_values: dict[str, Any],
) -> tuple[Any, str | None]:
    """Execute the configured operation. Returns (result, error_message)."""
    operation = field_config["operation"]
    databases = looker_state.get("databases", {})
    database_alias = verifier_values.get("database_alias")

    # SQL query operations
    if operation == "sql_query":
        sql = str(verifier_values.get("sql") or "")
        if not sql:
            return None, "SQL query is required"
        return execute_sql(databases, database_alias, sql)

    if operation == "sql_query_rounded":
        sql = str(verifier_values.get("sql") or "")
        decimal_places = int(verifier_values.get("decimal_places", 2) or 2)
        if not sql:
            return None, "SQL query is required"
        result, error = execute_sql(databases, database_alias, sql)
        if error:
            return None, error
        try:
            return round(float(result), decimal_places), None
        except (ValueError, TypeError):
            return result, None

    # Row count operations
    if operation == "row_count":
        table_name = str(verifier_values.get("table_name") or "")
        return get_row_count(databases, database_alias, table_name)

    if operation == "row_count_condition":
        table_name = str(verifier_values.get("table_name") or "")
        condition = str(verifier_values.get("condition") or "")
        return get_row_count(databases, database_alias, table_name, condition)

    # Aggregate operations
    if operation == "aggregate":
        table_name = str(verifier_values.get("table_name") or "")
        field = str(verifier_values.get("field") or "")
        agg_func = field_config.get("agg_func", "AVG")
        return get_aggregate(databases, database_alias, table_name, field, agg_func)

    if operation == "aggregate_condition":
        table_name = str(verifier_values.get("table_name") or "")
        field = str(verifier_values.get("field") or "")
        condition = str(verifier_values.get("condition") or "")
        agg_func = field_config.get("agg_func", "AVG")
        return get_aggregate(
            databases, database_alias, table_name, field, agg_func, condition
        )

    # Field value operations
    if operation == "field_value":
        table_name = str(verifier_values.get("table_name") or "")
        field = str(verifier_values.get("field") or "")
        key_field = str(verifier_values.get("key_field") or "")
        key_value = str(verifier_values.get("key_value") or "")
        return get_field_value(
            databases, database_alias, table_name, field, key_field, key_value
        )

    # Table metadata operations
    if operation == "table_exists":
        table_name = str(verifier_values.get("table_name") or "")
        tables = looker_state.get("tables", {})
        for db_alias, db_tables in tables.items():
            if database_alias and db_alias != database_alias:
                continue
            if table_name in db_tables:
                return True, None
        return False, None

    if operation == "table_row_count":
        table_name = str(verifier_values.get("table_name") or "")
        row_counts = looker_state.get("row_counts", {})
        for db_alias, db_counts in row_counts.items():
            if database_alias and db_alias != database_alias:
                continue
            if table_name in db_counts:
                return db_counts[table_name], None
        return None, f"Table '{table_name}' not found in metadata"

    return None, f"Unknown operation: {operation}"


async def looker_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify Looker SQL query results using dropdown + value interface."""
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Validate helper results available
    if not input.helper_results:
        raise ValueError("Helper results not available")

    # Get field selection
    field_to_check = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check:
        raise ValueError("field_to_check is required")

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        raise ValueError("expected_value is required")

    # Get field configuration (strip arrow hint if present for lookup)
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

    # Get Looker state from helper
    looker_state = input.helper_results.get(HelperIds.LOOKER_STATE)
    if not looker_state:
        raise ValueError("Looker state not available from helper")

    # Execute the operation
    try:
        result, error = _execute_operation(
            looker_state, field_config, input.verifier.verifier_values
        )
    except Exception as e:
        logger.error(f"Error executing field check: {e}")
        raise ValueError(f"Field check failed: {str(e)}") from e

    if error:
        raise ValueError(f"Operation failed: {error}")

    logger.info(
        f"Looker field check: {field_to_check} = {result} (expected: {expected_value})"
    )

    # Get tolerance for numeric comparison
    tolerance = None
    tolerance_str = input.verifier.verifier_values.get("tolerance")
    if tolerance_str:
        try:
            tolerance = Decimal(str(tolerance_str))
        except (ValueError, TypeError, InvalidOperation):
            pass

    matches = compare_values(result, expected_value, tolerance)
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
