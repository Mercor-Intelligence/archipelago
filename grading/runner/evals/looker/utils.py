"""Utility functions for Looker field checks.

Supports both DuckDB (preferred for Looker offline mode) and SQLite (legacy).
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger


def _execute_query(
    conn: Any, sql: str, params: tuple[Any, ...] | None = None
) -> tuple[list[Any], list[str]]:
    """Execute query on connection, handling both DuckDB and SQLite."""
    if params:
        cursor = conn.execute(sql, params)
    else:
        cursor = conn.execute(sql)

    # Get column names
    if hasattr(cursor, "description") and cursor.description:
        columns = [desc[0] for desc in cursor.description]
    else:
        columns = []

    rows = cursor.fetchall()
    return rows, columns


def execute_sql(
    databases: dict[str, Any],
    database_alias: str | None,
    sql: str,
) -> tuple[Any, str | None]:
    """Execute SQL query against a database.

    Returns:
        Tuple of (result, error_message)
        - result: Query result (single value, list, or None)
        - error_message: Error message if query failed, None otherwise
    """
    if not databases:
        return None, "No databases available"

    def _process_result(rows: list[Any], columns: list[str]) -> Any:
        """Process query result into appropriate format."""
        if not rows:
            return None
        if len(rows) == 1 and len(columns) == 1:
            return rows[0][0]
        if len(rows) == 1:
            return dict(zip(columns, rows[0], strict=False))
        return [dict(zip(columns, row, strict=False)) for row in rows]

    # If specific alias provided, use that database
    if database_alias:
        db_info = databases.get(database_alias)
        if not db_info:
            available = list(databases.keys())
            return (
                None,
                f"Database '{database_alias}' not found. Available: {available}",
            )

        conn = db_info.get("connection")
        if not conn:
            return None, f"No connection for database '{database_alias}'"

        try:
            rows, columns = _execute_query(conn, sql)
            return _process_result(rows, columns), None
        except Exception as e:
            return None, f"SQL error: {str(e)}"

    # Try all databases if no alias specified
    errors = []
    query_succeeded_but_empty = False

    for alias, db_info in databases.items():
        conn = db_info.get("connection")
        if not conn:
            continue

        try:
            rows, columns = _execute_query(conn, sql)
            if not rows:
                query_succeeded_but_empty = True
                continue

            result = _process_result(rows, columns)
            logger.info(f"Query succeeded on database '{alias}'")
            return result, None

        except Exception as e:
            errors.append(f"{alias}: {str(e)}")
            continue

    if query_succeeded_but_empty:
        return None, None

    if errors:
        return None, f"SQL failed on all databases: {'; '.join(errors)}"

    return None, "No databases could execute the query"


def get_row_count(
    databases: dict[str, Any],
    database_alias: str | None,
    table_name: str,
    condition: str | None = None,
) -> tuple[int | None, str | None]:
    """Get row count for a table, optionally filtered by condition."""
    sql = f'SELECT COUNT(*) FROM "{table_name}"'  # noqa: S608
    if condition:
        sql += f" WHERE {condition}"

    result, error = execute_sql(databases, database_alias, sql)
    if error:
        return None, error

    try:
        return int(result), None
    except (ValueError, TypeError):
        return None, f"Invalid count result: {result}"


def get_aggregate(
    databases: dict[str, Any],
    database_alias: str | None,
    table_name: str,
    field: str,
    agg_func: str,
    condition: str | None = None,
) -> tuple[Any, str | None]:
    """Get aggregate value (AVG, SUM, MIN, MAX, COUNT) for a field."""
    agg_func_upper = agg_func.upper()
    if agg_func_upper not in ("AVG", "SUM", "MIN", "MAX", "COUNT"):
        return None, f"Invalid aggregate function: {agg_func}"

    sql = f'SELECT {agg_func_upper}("{field}") FROM "{table_name}"'  # noqa: S608
    if condition:
        sql += f" WHERE {condition}"

    return execute_sql(databases, database_alias, sql)


def get_field_value(
    databases: dict[str, Any],
    database_alias: str | None,
    table_name: str,
    field: str,
    key_field: str,
    key_value: str,
) -> tuple[Any, str | None]:
    """Get a specific field value by key lookup."""
    if not databases:
        return None, "No databases available"

    if database_alias and database_alias not in databases:
        available = list(databases.keys())
        return None, f"Database '{database_alias}' not found. Available: {available}"

    sql = f'SELECT "{field}" FROM "{table_name}" WHERE "{key_field}" = ?'  # noqa: S608

    for alias, db_info in databases.items():
        if database_alias and alias != database_alias:
            continue

        conn = db_info.get("connection")
        if not conn:
            continue

        try:
            rows, _ = _execute_query(conn, sql, (key_value,))
            if rows:
                return rows[0][0], None
        except Exception:
            continue

    return None, f"No row found where {key_field} = {key_value}"


def compare_values(
    actual: Any,
    expected: Any,
    tolerance: Decimal | None = None,
) -> bool:
    """Compare values with optional numeric tolerance."""
    if actual is None:
        return False

    # Boolean comparison (e.g., table_exists returns True/False)
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
