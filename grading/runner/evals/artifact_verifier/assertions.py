"""Assertion evaluation functions for artifact verification.

Evaluates typed assertions against ArtifactState tables, returning
detailed results for each assertion including pass/fail status,
expected vs actual values, and any errors encountered.
"""

from typing import Any

from pydantic import BaseModel

from runner.helpers.artifact_state.models import ArtifactState


class AssertionResult(BaseModel):
    """Result of evaluating a single assertion."""

    assertion_type: str
    passed: bool
    table: str
    expected: Any
    actual: Any | None = None
    error: str | None = None
    matching_rows: int = 0


def evaluate_assertion(
    state: ArtifactState, assertion: dict[str, Any]
) -> AssertionResult:
    """Evaluate a single assertion against ArtifactState.

    Args:
        state: The parsed artifact state containing tables.
        assertion: Assertion definition with type, table, and type-specific fields.

    Returns:
        AssertionResult with pass/fail status and details.
    """
    a_type = assertion.get("type", "")
    table_name = assertion.get("table", "")
    where = assertion.get("where") or {}

    # Get table rows
    rows = state.tables.get(table_name, [])
    if not rows and table_name not in state.tables:
        return AssertionResult(
            assertion_type=a_type,
            passed=False,
            table=table_name,
            expected=assertion,
            error=f"Table '{table_name}' not found. Available: {list(state.tables.keys())}",
        )

    # Filter by where clause
    matching_rows = [r for r in rows if _matches_where(r, where)]

    if a_type == "row_exists":
        return _eval_row_exists(table_name, where, matching_rows)

    elif a_type == "field_equals":
        return _eval_field_equals(assertion, table_name, where, matching_rows)

    elif a_type == "row_count":
        return _eval_row_count(assertion, table_name, matching_rows)

    elif a_type == "list_contains":
        return _eval_list_contains(assertion, table_name, where, matching_rows)

    elif a_type == "list_contains_all":
        return _eval_list_contains_all(assertion, table_name, where, matching_rows)

    else:
        return AssertionResult(
            assertion_type=a_type,
            passed=False,
            table=table_name,
            expected=assertion,
            error=f"Unknown assertion type: {a_type}",
        )


def _matches_where(row: dict[str, Any], where: dict[str, Any]) -> bool:
    """Check if a row matches all conditions in the where clause."""
    for field, expected in where.items():
        actual = row.get(field)
        if not values_equal(actual, expected):
            return False
    return True


def values_equal(actual: Any, expected: Any) -> bool:
    """Compare values with type coercion for common mismatches.

    Handles:
    - Exact equality
    - Boolean comparison (True == "true", False == "false")
    - String/number comparisons (e.g., "100" == 100)
    - Case-insensitive string comparison
    - None handling
    """
    if actual == expected:
        return True

    # None handling
    if actual is None or expected is None:
        return False

    # Boolean comparison - handle separately to avoid bool being treated as int
    # (Python's bool is a subclass of int, so float(True) == 1.0)
    # Only match explicit boolean string representations, not "1"/"0"
    if isinstance(actual, bool):
        expected_str = str(expected).lower()
        if expected_str in ("true", "yes", "t"):
            return actual is True
        elif expected_str in ("false", "no", "f"):
            return actual is False
        return False
    if isinstance(expected, bool):
        actual_str = str(actual).lower()
        if actual_str in ("true", "yes", "t"):
            return expected is True
        elif actual_str in ("false", "no", "f"):
            return expected is False
        return False

    # Try numeric comparison
    try:
        if float(actual) == float(expected):
            return True
    except (ValueError, TypeError):
        pass

    # Case-insensitive string comparison
    if isinstance(actual, str) and isinstance(expected, str):
        if actual.lower() == expected.lower():
            return True

    # String coercion comparison
    if str(actual) == str(expected):
        return True

    return False


def _compare_count(count: int, operator: str, value: Any) -> bool:
    """Compare a count value using the specified operator.

    Args:
        count: The actual count (always int).
        operator: Comparison operator (eq, gt, lt, gte, lte).
        value: Expected value from config (may be string, coerced to int).

    Returns:
        True if the comparison passes, False otherwise.
    """
    # Coerce value to int to handle string inputs from config
    try:
        int_value = int(value)
    except (ValueError, TypeError):
        return False

    if operator == "eq":
        return count == int_value
    elif operator == "gt":
        return count > int_value
    elif operator == "lt":
        return count < int_value
    elif operator == "gte":
        return count >= int_value
    elif operator == "lte":
        return count <= int_value
    else:
        return False


def _eval_row_exists(
    table_name: str,
    where: dict[str, Any],
    matching_rows: list[dict[str, Any]],
) -> AssertionResult:
    """Evaluate row_exists assertion."""
    return AssertionResult(
        assertion_type="row_exists",
        passed=len(matching_rows) > 0,
        table=table_name,
        expected=where,
        actual=f"{len(matching_rows)} matching rows",
        matching_rows=len(matching_rows),
    )


def _eval_field_equals(
    assertion: dict[str, Any],
    table_name: str,
    where: dict[str, Any],
    matching_rows: list[dict[str, Any]],
) -> AssertionResult:
    """Evaluate field_equals assertion."""
    field = assertion.get("field") or ""
    expected = assertion.get("expected")

    if not matching_rows:
        return AssertionResult(
            assertion_type="field_equals",
            passed=False,
            table=table_name,
            expected=expected,
            error=f"No rows match where clause: {where}",
        )

    actual = matching_rows[0].get(field)
    return AssertionResult(
        assertion_type="field_equals",
        passed=values_equal(actual, expected),
        table=table_name,
        expected=expected,
        actual=actual,
        matching_rows=len(matching_rows),
    )


def _eval_row_count(
    assertion: dict[str, Any],
    table_name: str,
    matching_rows: list[dict[str, Any]],
) -> AssertionResult:
    """Evaluate row_count assertion."""
    operator = assertion.get("operator") or "eq"
    value = assertion.get("value") or 0
    count = len(matching_rows)
    passed = _compare_count(count, operator, value)

    return AssertionResult(
        assertion_type="row_count",
        passed=passed,
        table=table_name,
        expected=f"{operator} {value}",
        actual=count,
        matching_rows=count,
    )


def _eval_list_contains(
    assertion: dict[str, Any],
    table_name: str,
    where: dict[str, Any],
    matching_rows: list[dict[str, Any]],
) -> AssertionResult:
    """Evaluate list_contains assertion."""
    field = assertion.get("field") or ""
    value = assertion.get("value")

    if not matching_rows:
        return AssertionResult(
            assertion_type="list_contains",
            passed=False,
            table=table_name,
            expected=value,
            error=f"No rows match where clause: {where}",
        )

    actual_list = matching_rows[0].get(field, [])
    if not isinstance(actual_list, list):
        actual_list = [actual_list] if actual_list is not None else []

    # Check with type coercion
    contains = any(values_equal(item, value) for item in actual_list)

    return AssertionResult(
        assertion_type="list_contains",
        passed=contains,
        table=table_name,
        expected=value,
        actual=actual_list,
        matching_rows=len(matching_rows),
    )


def _eval_list_contains_all(
    assertion: dict[str, Any],
    table_name: str,
    where: dict[str, Any],
    matching_rows: list[dict[str, Any]],
) -> AssertionResult:
    """Evaluate list_contains_all assertion."""
    field = assertion.get("field") or ""
    values = assertion.get("values") or []

    if not matching_rows:
        return AssertionResult(
            assertion_type="list_contains_all",
            passed=False,
            table=table_name,
            expected=values,
            error=f"No rows match where clause: {where}",
        )

    actual_list = matching_rows[0].get(field, [])
    if not isinstance(actual_list, list):
        actual_list = [actual_list] if actual_list is not None else []

    # Check all values are contained (with type coercion)
    contains_all = all(
        any(values_equal(item, v) for item in actual_list) for v in values
    )

    return AssertionResult(
        assertion_type="list_contains_all",
        passed=contains_all,
        table=table_name,
        expected=values,
        actual=actual_list,
        matching_rows=len(matching_rows),
    )
