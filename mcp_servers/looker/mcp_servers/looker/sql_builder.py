"""SQL query building utilities for DuckDB.

This module consolidates SQL generation functions used across the codebase,
providing safe query building with proper escaping and type validation.

Functions handle:
- Looker filter expression to SQL conversion
- SQL identifier quoting
- String escaping
- Numeric validation
- WHERE clause generation
"""

import re


def looker_field_to_column(field: str) -> str:
    """Extract column name from Looker field (view_name.column_name -> column_name).

    Args:
        field: Looker-style field name like 'service_requests.agency'

    Returns:
        Column name like 'agency'
    """
    if "." in field:
        return field.split(".", 1)[1]
    return field


def quote_identifier(name: str) -> str:
    """Safely quote a SQL identifier by escaping embedded double quotes.

    Args:
        name: SQL identifier (column name, table name, etc.)

    Returns:
        Quoted identifier safe for use in SQL
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def validate_numeric(value: str) -> str | None:
    """Validate and sanitize a numeric value for SQL.

    Args:
        value: String that should be a number

    Returns:
        Sanitized numeric string, or None if not a valid number
    """
    value = value.strip()
    try:
        # Try to parse as float to validate
        float(value)
        # Return the original string (preserves int vs float)
        return value
    except ValueError:
        return None


def escape_sql_string(value: str) -> str:
    """Escape a string value for safe inclusion in SQL single-quoted literals.

    Args:
        value: String to escape

    Returns:
        Escaped string (single quotes doubled)
    """
    return value.replace("'", "''")


def looker_expr_to_sql(field: str, expression: str) -> str:
    """Convert a Looker filter expression to SQL clause.

    Handles Looker filter syntax including:
    - NULL/EMPTY checks
    - NOT prefix and - negation
    - Range brackets [N, M], (N, M), etc.
    - Comparison operators (>=, <=, >, <)
    - Date ranges (value1 to value2)
    - Wildcards (%)
    - Date keywords (before, after)
    - Exact matches

    Args:
        field: Column name (already extracted from Looker field)
        expression: Looker filter expression

    Returns:
        SQL clause string
    """
    expr = expression.strip()
    qf = quote_identifier(field)

    # Handle NULL/EMPTY checks (parenthesized to preserve precedence when combined with AND)
    # Note: We avoid comparing numeric columns to '' as DuckDB will error on type mismatch.
    # Using CAST to VARCHAR for the empty string check ensures compatibility with all types.
    if expr.upper() == "NULL" or expr.upper() == "EMPTY":
        return f"({qf} IS NULL OR CAST({qf} AS VARCHAR) = '')"

    if expr.upper() == "NOT NULL" or expr.upper() == "-EMPTY":
        return f"({qf} IS NOT NULL AND CAST({qf} AS VARCHAR) != '')"

    # Handle NOT prefix
    if expr.upper().startswith("NOT ") and not expr.upper().startswith("NOT NULL"):
        inner_expr = expr[4:].strip()
        inner_sql = looker_expr_to_sql(field, inner_expr)
        return f"NOT ({inner_sql})"

    # Handle negation with - prefix (Looker syntax: -value means NOT value)
    # But only for non-numeric values (don't treat -5 as NOT 5)
    if expr.startswith("-") and validate_numeric(expr) is None:
        inner_expr = expr[1:]
        inner_sql = looker_expr_to_sql(field, inner_expr)
        return f"NOT ({inner_sql})"

    # Handle range brackets [N, M] with proper inclusive/exclusive handling
    range_match = re.match(r"^([\[\(])(.+?),\s*(.+?)([\]\)])$", expr)
    if range_match:
        left_bracket = range_match.group(1)
        low = validate_numeric(range_match.group(2))
        high = validate_numeric(range_match.group(3))
        right_bracket = range_match.group(4)

        # If not valid numbers, treat as string comparison
        if low is None or high is None:
            escaped_expr = escape_sql_string(expr)
            return f"{qf} = '{escaped_expr}'"

        left_inclusive = left_bracket == "["
        right_inclusive = right_bracket == "]"

        if left_inclusive and right_inclusive:
            return f"{qf} BETWEEN {low} AND {high}"
        else:
            left_op = ">=" if left_inclusive else ">"
            right_op = "<=" if right_inclusive else "<"
            return f"({qf} {left_op} {low} AND {qf} {right_op} {high})"

    # Handle comparison operators (validate numeric values to prevent SQL injection)
    if expr.startswith(">="):
        value = validate_numeric(expr[2:])
        if value is not None:
            return f"{qf} >= {value}"
        # Fall through to exact match if not numeric
    if expr.startswith("<="):
        value = validate_numeric(expr[2:])
        if value is not None:
            return f"{qf} <= {value}"
    if expr.startswith(">") and not expr.startswith(">="):
        value = validate_numeric(expr[1:])
        if value is not None:
            return f"{qf} > {value}"
    if expr.startswith("<") and not expr.startswith("<="):
        value = validate_numeric(expr[1:])
        if value is not None:
            return f"{qf} < {value}"

    # Handle date range "value1 to value2" (case-insensitive keyword, preserve value case)
    if " to " in expr.lower():
        # Find " to " case-insensitively without lowercasing the values
        to_idx = expr.lower().index(" to ")
        low = escape_sql_string(expr[:to_idx].strip())
        high = escape_sql_string(expr[to_idx + 4 :].strip())
        return f"{qf} BETWEEN '{low}' AND '{high}'"

    # Handle wildcards (convert to LIKE)
    if "%" in expr:
        return f"{qf} ILIKE '{escape_sql_string(expr)}'"

    # Handle date keywords
    lower_expr = expr.lower()
    if lower_expr.startswith("before "):
        return f"{qf} < '{escape_sql_string(expr[7:].strip())}'"
    if lower_expr.startswith("after "):
        return f"{qf} > '{escape_sql_string(expr[6:].strip())}'"

    # Exact match
    return f"{qf} = '{escape_sql_string(expr)}'"


def build_where_clause(filters: dict[str, list[str]], extract_column: bool = True) -> str:
    """Build SQL WHERE clause from Looker filters.

    Args:
        filters: Dict mapping field names to list of filter expressions
        extract_column: If True, extract column name from Looker field (view.column -> column).
                       If False, use field name as-is.

    Returns:
        SQL WHERE clause (without WHERE keyword) or empty string
    """
    if not filters:
        return ""

    clauses = []
    for field, expressions in filters.items():
        if not expressions:
            continue

        # Extract column name from Looker field if requested
        column = looker_field_to_column(field) if extract_column else field

        if len(expressions) == 1:
            clauses.append(looker_expr_to_sql(column, expressions[0]))
        else:
            # Multiple expressions for same field: combine with OR
            field_clauses = [looker_expr_to_sql(column, expr) for expr in expressions]
            clauses.append(f"({' OR '.join(field_clauses)})")

    return " AND ".join(clauses)


def convert_filters_to_dict(filters: list) -> dict[str, list[str]]:
    """Convert filter list to dict with list values.

    Handles duplicate field names by accumulating values into lists.
    Skips filters with empty values.

    Args:
        filters: List of filter objects (with field and value attributes) or dicts

    Returns:
        Dict mapping field names to lists of values
    """
    filter_dict: dict[str, list[str]] = {}
    for f in filters:
        # Handle both dict and object with attributes
        if isinstance(f, dict):
            field = f.get("field")
            value = f.get("value")
        else:
            field = f.field
            value = f.value
        # Skip filters with empty field or value
        if not field or not value:
            continue
        if field not in filter_dict:
            filter_dict[field] = []
        filter_dict[field].append(value)
    return filter_dict


def parse_sort_field(sort: str) -> tuple[str, str]:
    """Parse a Looker sort field into column and direction.

    Handles both formats:
    - "-field_name" (descending)
    - "field_name desc/asc"

    Args:
        sort: Sort specification string

    Returns:
        Tuple of (column_name, direction) where direction is 'ASC' or 'DESC'
    """
    sort_lower = sort.lower()

    if sort.startswith("-"):
        # Format: -field_name means descending
        return (looker_field_to_column(sort[1:]), "DESC")
    elif sort_lower.endswith(" desc"):
        # Format: field_name desc
        return (looker_field_to_column(sort[:-5].strip()), "DESC")
    elif sort_lower.endswith(" asc"):
        # Format: field_name asc
        return (looker_field_to_column(sort[:-4].strip()), "ASC")
    else:
        # Default to ascending
        return (looker_field_to_column(sort), "ASC")
