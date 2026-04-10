"""Seeded data module for DuckDB-backed query execution.

This module handles executing queries against DuckDB, which serves as the
single source of truth for all data. Queries are executed directly on DuckDB
using SQL, rather than loading data into Python memory.

Data can come from:
  1. Bundled data (pre-seeded in the DuckDB database)
  2. User-uploaded CSVs (loaded into DuckDB at import time)

CSV files should have Looker-style headers:
  - Headers use format: view_name.field_name (e.g., service_requests.agency)
  - Each CSV file represents one view/explore
"""

import csv
import re
from pathlib import Path
from typing import Any

import duckdb
from loguru import logger
from sql_builder import (
    build_where_clause,
    looker_field_to_column,
    parse_sort_field,
    quote_identifier,
)

# Cache for loaded CSV data (view_name -> list of rows)
# Kept for backward compatibility with CSV fallback
_csv_data_cache: dict[str, list[dict[str, Any]]] = {}


# =============================================================================
# DuckDB Query Execution (Primary Method)
# =============================================================================


def _get_duckdb_connection() -> duckdb.DuckDBPyConnection | None:
    """Get a read-only connection to the runtime DuckDB.

    Returns:
        DuckDB connection or None if database doesn't exist or can't be opened
    """
    from data_layer import get_runtime_duckdb_path

    db_path = get_runtime_duckdb_path()
    if not db_path.exists():
        return None
    try:
        return duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as e:
        logger.warning(f"Failed to connect to DuckDB at {db_path}: {e}")
        return None


def _get_base_column_for_measure(measure_field: str, agg_type: str) -> str | None:
    """Extract base column from derived measure field name.

    For auto-generated measures like 'view.field_sum', extract 'field'.
    Delegates to _get_base_field_for_measure and strips the view prefix.

    Args:
        measure_field: Full measure field name (e.g., 'purchases.price_sum')
        agg_type: Aggregation type (e.g., 'sum', 'average')

    Returns:
        Base column name or None if not a derived measure
    """
    base_field = _get_base_field_for_measure(measure_field, agg_type)
    if base_field:
        return looker_field_to_column(base_field)
    return None


def _build_sql_query(
    table_name: str,
    fields: list[str],
    filters: dict[str, list[str]] | None,
    sorts: list[str] | None,
    limit: int,
    measures: dict[str, str] | None,
) -> str:
    """Build SQL query for DuckDB execution.

    Args:
        table_name: DuckDB table name
        fields: List of Looker-style fields
        filters: Dict of filters
        sorts: List of sort specs
        limit: Row limit
        measures: Dict mapping measure fields to aggregation type

    Returns:
        SQL query string
    """
    measures = measures or {}

    # Separate dimensions from measures
    dimension_fields = [f for f in fields if f not in measures]
    measure_fields = [f for f in fields if f in measures]

    # Build SELECT clause
    select_parts = []

    for field in dimension_fields:
        column = looker_field_to_column(field)
        select_parts.append(quote_identifier(column))

    for field in measure_fields:
        column = looker_field_to_column(field)
        agg_type = measures[field]

        # Get base column for derived measures (e.g., price_sum -> price)
        base_column = _get_base_column_for_measure(field, agg_type)
        source_column = base_column if base_column else column

        q_col = quote_identifier(column)
        q_src = quote_identifier(source_column)

        if agg_type == "count":
            select_parts.append(f"COUNT(*) AS {q_col}")
        elif agg_type == "sum":
            select_parts.append(f"SUM({q_src}) AS {q_col}")
        elif agg_type in ("average", "avg"):
            select_parts.append(f"AVG({q_src}) AS {q_col}")
        elif agg_type == "min":
            select_parts.append(f"MIN({q_src}) AS {q_col}")
        elif agg_type == "max":
            select_parts.append(f"MAX({q_src}) AS {q_col}")
        elif agg_type == "count_distinct":
            select_parts.append(f"COUNT(DISTINCT {q_src}) AS {q_col}")
        else:
            # Unknown aggregate type - default to COUNT to avoid invalid SQL with GROUP BY
            logger.warning(f"Unknown measure type '{agg_type}' for {field}, defaulting to COUNT")
            select_parts.append(f"COUNT(*) AS {q_col}")

    if not select_parts:
        select_parts = ["*"]

    q_table = quote_identifier(table_name)
    sql = f"SELECT {', '.join(select_parts)} FROM {q_table}"

    # Split filters into dimension filters (WHERE) and measure filters (HAVING).
    # Measure filters reference aggregate aliases that don't exist as raw columns,
    # so they must go in HAVING (after GROUP BY), not WHERE (before GROUP BY).
    all_filters = filters or {}
    dim_filters = {k: v for k, v in all_filters.items() if k not in measures}
    measure_filters = {k: v for k, v in all_filters.items() if k in measures}

    # Add WHERE clause (dimension filters only)
    where_clause = build_where_clause(dim_filters)
    if where_clause:
        sql += f" WHERE {where_clause}"

    # Add GROUP BY if there are measures and dimensions
    if measures and dimension_fields:
        group_by_columns = [quote_identifier(looker_field_to_column(f)) for f in dimension_fields]
        sql += f" GROUP BY {', '.join(group_by_columns)}"

    # Add HAVING clause (measure filters — applied after aggregation)
    if measure_filters:
        having_clause = build_where_clause(measure_filters)
        if having_clause:
            sql += f" HAVING {having_clause}"

    # Add ORDER BY
    if sorts:
        order_parts = []
        for sort in sorts:
            column, direction = parse_sort_field(sort)
            order_parts.append(f"{quote_identifier(column)} {direction}")
        sql += f" ORDER BY {', '.join(order_parts)}"

    # Add LIMIT
    sql += f" LIMIT {limit}"

    return sql


def _execute_duckdb_query(
    table_name: str,
    fields: list[str],
    filters: dict[str, list[str]] | None = None,
    sorts: list[str] | None = None,
    limit: int = 5000,
    measures: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
    """Execute a query directly on DuckDB.

    This is the primary query execution method. Returns results with
    Looker-style field names (view_name.column_name).

    Args:
        table_name: DuckDB table name (same as view name)
        fields: List of Looker-style fields
        filters: Dict of filters
        sorts: List of sort specs
        limit: Row limit
        measures: Dict mapping measure fields to aggregation type

    Returns:
        List of result dicts with Looker-style keys, or None if table not found
    """
    conn = _get_duckdb_connection()
    if not conn:
        logger.debug("No DuckDB connection available")
        return None

    try:
        # Check if table exists
        tables_result = conn.execute("SHOW TABLES").fetchall()
        tables = [row[0] for row in tables_result]
        if table_name not in tables:
            logger.debug(f"Table {table_name} not found in DuckDB")
            return None

        # Build and execute SQL
        sql = _build_sql_query(table_name, fields, filters, sorts, limit, measures)
        logger.debug(f"Executing DuckDB query: {sql}")

        try:
            result = conn.execute(sql)
        except duckdb.Error as e:
            # Table exists but query failed (e.g., bad column name,
            # type mismatch). Return empty list, NOT None, so the
            # caller doesn't fall back to mock data.
            logger.warning(f"DuckDB query failed: {e}\n  SQL: {sql}")
            return []

        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()

        # Convert to list of dicts with Looker-style keys
        data = []
        for row in rows:
            row_dict = {}
            for i, col in enumerate(columns):
                # Convert column name back to Looker field format
                looker_field = f"{table_name}.{col}"
                value = row[i]
                # Convert numeric types to Python native types
                if hasattr(value, "item"):  # numpy types
                    value = value.item()
                row_dict[looker_field] = value
            data.append(row_dict)

        logger.debug(f"DuckDB query returned {len(data)} rows")
        return data

    except Exception as e:
        logger.warning(f"DuckDB connection/table lookup failed: {e}")
        return None
    finally:
        conn.close()


# =============================================================================
# CSV Fallback Functions (Kept for backward compatibility)
# =============================================================================


def _sanitize_field_name(field_name: str) -> str:
    """Sanitize a field name to match LookML/DuckDB conventions.

    This must match the sanitization in lookml_generator.py and build_duckdb.py
    to ensure field names are consistent across all layers.

    Args:
        field_name: Raw field name from CSV header

    Returns:
        Sanitized field name (lowercase, underscores)
    """
    # Replace spaces and special chars with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", field_name)
    # Remove consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")
    # Ensure it starts with a letter or underscore
    if sanitized and sanitized[0].isdigit():
        sanitized = f"col_{sanitized}"
    # Default name if empty
    if not sanitized:
        sanitized = "unnamed_column"
    return sanitized.lower()


def _get_csv_directories() -> list[Path]:
    """Get all directories to search for CSV files.

    Returns directories in priority order:
    1. User CSV dir (STATE_LOCATION in production, session temp dir in local dev)
    2. Bundled data/csv (pre-seeded data shipped with repo, read-only)

    Note: We intentionally do NOT check workspace-relative paths like .apps_data/
    because in production those resolve through symlinks to a shared cache,
    which would leak data between users.

    Returns:
        List of existing directories containing CSV files
    """
    from data_layer import get_user_csv_dir

    dirs = []

    # Check user CSV dir first (STATE_LOCATION in prod, session temp dir in local dev)
    user_csv_dir = get_user_csv_dir()
    if user_csv_dir and user_csv_dir.exists():
        dirs.append(user_csv_dir)

    # Check bundled data/csv (pre-seeded data shipped with repo)
    # This is read-only bundled data, safe to share
    bundled_csv = Path(__file__).parent / "data" / "csv"
    if bundled_csv.exists() and bundled_csv not in dirs:
        dirs.append(bundled_csv)

    return dirs


def _find_csv_path(view_name: str) -> Path | None:
    """Find CSV file for a view across all data directories.

    User data directories are checked first, allowing user uploads
    to override bundled data.

    Args:
        view_name: The view/explore name (e.g., 'service_requests')

    Returns:
        Path to CSV file if found, None otherwise
    """
    for csv_dir in _get_csv_directories():
        csv_path = csv_dir / f"{view_name}.csv"
        if csv_path.exists():
            return csv_path
    return None


def get_available_seeded_views() -> list[str]:
    """Get list of views that have seeded CSV data available.

    Checks all configured directories (STATE_LOCATION, .apps_data, bundled data).

    Returns:
        List of view names (derived from CSV filenames)
    """
    views = set()
    for csv_dir in _get_csv_directories():
        for f in csv_dir.glob("*.csv"):
            views.add(f.stem)
    return list(views)


def has_seeded_data(view_name: str) -> bool:
    """Check if a view has seeded CSV data available.

    Checks all configured directories (STATE_LOCATION, .apps_data, bundled data).

    Args:
        view_name: The view/explore name (e.g., 'service_requests')

    Returns:
        True if CSV data exists for this view in any directory
    """
    return _find_csv_path(view_name) is not None


def load_seeded_data(view_name: str) -> list[dict[str, Any]]:
    """Load CSV data for a given view name.

    Data is cached after first load for performance.
    Checks all configured directories (STATE_LOCATION, .apps_data, bundled data).

    CSV headers are normalized to Looker-style field names:
    - Raw headers like "User ID" become "view_name.user_id"
    - Headers already in Looker format (view.field) are preserved
    - This ensures consistency with LookML field names

    Args:
        view_name: The view/explore name (e.g., 'service_requests')

    Returns:
        List of row dictionaries with Looker-style field names as keys
        (e.g., {'uk_retail_ab_test.user_id': '123', ...})

    Raises:
        FileNotFoundError: If CSV file doesn't exist for view
    """
    if view_name in _csv_data_cache:
        return _csv_data_cache[view_name]

    csv_path = _find_csv_path(view_name)
    if not csv_path:
        raise FileNotFoundError(f"No seeded data for view: {view_name}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        raw_data = list(reader)

    # Normalize headers to Looker-style field names (view_name.field_name)
    # This ensures queries using fields like "uk_retail_ab_test.device" work
    data = []
    for row in raw_data:
        normalized_row = {}
        for raw_key, value in row.items():
            # Check if already in Looker format (has view prefix)
            if "." in raw_key and raw_key.split(".")[0] == view_name:
                # Already has correct view prefix - just sanitize the field part
                parts = raw_key.split(".", 1)
                sanitized_field = _sanitize_field_name(parts[1])
                normalized_key = f"{view_name}.{sanitized_field}"
            elif "." in raw_key:
                # Has a different view prefix - sanitize and keep as-is
                parts = raw_key.split(".", 1)
                sanitized_field = _sanitize_field_name(parts[1])
                normalized_key = f"{parts[0]}.{sanitized_field}"
            else:
                # Plain header - sanitize and add view prefix
                sanitized_field = _sanitize_field_name(raw_key)
                normalized_key = f"{view_name}.{sanitized_field}"
            normalized_row[normalized_key] = value
        data.append(normalized_row)

    _csv_data_cache[view_name] = data
    return data


def clear_cache() -> None:
    """Clear the CSV data cache. Useful for testing."""
    _csv_data_cache.clear()


def _get_view_from_fields(fields: list[str]) -> str | None:
    """Extract view name from field list.

    Args:
        fields: List of field names like ['service_requests.agency', 'service_requests.count']

    Returns:
        View name if all fields share same view and all have view prefix, None otherwise
    """
    if not fields:
        return None

    views = set()
    for field in fields:
        if "." in field:
            views.add(field.split(".")[0])
        else:
            # Field without view prefix - can't determine view
            return None

    # Only return if all fields are from the same view
    if len(views) == 1:
        return views.pop()
    return None


def _parse_looker_filter(expression: str, row_value: Any) -> bool:
    """Parse and evaluate a Looker filter expression against a row value.

    Supports Looker filter syntax:
    - Exact match: "value"
    - Negation: "-value" or "NOT value"
    - Comparison: ">N", ">=N", "<N", "<=N"
    - Range: "[N, M]" (inclusive), "(N, M)" (exclusive)
    - Wildcards: "val%" (starts with), "%val" (ends with), "%val%" (contains)
    - Negated wildcards: "-%val%"
    - Null checks: "NULL", "NOT NULL", "EMPTY", "-EMPTY"
    - Date ranges: "value1 to value2"

    Args:
        expression: Looker filter expression string
        row_value: The value from the data row to compare against

    Returns:
        True if the row_value matches the filter expression
    """
    import re

    expr = expression.strip()
    str_value = str(row_value) if row_value is not None else ""

    # Handle NULL/EMPTY checks
    if expr.upper() == "NULL" or expr.upper() == "EMPTY":
        return row_value is None or str_value == "" or str_value.lower() == "null"

    if expr.upper() == "NOT NULL" or expr.upper() == "-EMPTY":
        return row_value is not None and str_value != "" and str_value.lower() != "null"

    # Handle NOT prefix (but not NOT NULL which is handled above)
    if expr.upper().startswith("NOT ") and not expr.upper().startswith("NOT NULL"):
        inner_expr = expr[4:].strip()
        return not _parse_looker_filter(inner_expr, row_value)

    # Handle negation with - prefix (Looker syntax: -value means NOT value)
    if expr.startswith("-"):
        inner_expr = expr[1:]
        return not _parse_looker_filter(inner_expr, row_value)

    # Handle range brackets [N, M] or (N, M)
    range_match = re.match(r"^[\[\(](.+?),\s*(.+?)[\]\)]$", expr)
    if range_match:
        try:
            # NULL values should not match numeric range filters
            if row_value is None or str_value == "" or str_value.lower() == "null":
                return False
            low = float(range_match.group(1).strip())
            high = float(range_match.group(2).strip())
            num_value = float(row_value)
            inclusive_low = expr.startswith("[")
            inclusive_high = expr.endswith("]")

            if inclusive_low and inclusive_high:
                return low <= num_value <= high
            elif inclusive_low:
                return low <= num_value < high
            elif inclusive_high:
                return low < num_value <= high
            else:
                return low < num_value < high
        except (ValueError, TypeError):
            return False

    # Handle comparison operators >=, <=, >, <
    if expr.startswith(">="):
        try:
            threshold = float(expr[2:].strip())
            return float(row_value) >= threshold if row_value is not None else False
        except (ValueError, TypeError):
            return False

    if expr.startswith("<="):
        try:
            threshold = float(expr[2:].strip())
            return float(row_value) <= threshold if row_value is not None else False
        except (ValueError, TypeError):
            return False

    if expr.startswith(">") and not expr.startswith(">="):
        try:
            threshold = float(expr[1:].strip())
            return float(row_value) > threshold if row_value is not None else False
        except (ValueError, TypeError):
            return False

    if expr.startswith("<") and not expr.startswith("<="):
        try:
            threshold = float(expr[1:].strip())
            return float(row_value) < threshold if row_value is not None else False
        except (ValueError, TypeError):
            return False

    # Handle date range "value1 to value2"
    if " to " in expr.lower():
        parts = expr.lower().split(" to ")
        if len(parts) == 2:
            start, end = parts[0].strip(), parts[1].strip()
            # Simple string comparison for dates (works for ISO format)
            return start <= str_value.lower() <= end

    # Handle wildcards
    if "%" in expr:
        # Convert Looker wildcard to regex, escaping metacharacters first
        # Note: % is not a regex metacharacter, so re.escape() leaves it as-is
        escaped = re.escape(expr)
        # Replace % with .* for wildcard matching
        pattern = escaped.replace("%", ".*")
        pattern = f"^{pattern}$"
        try:
            return bool(re.match(pattern, str_value, re.IGNORECASE))
        except re.error:
            return False

    # Handle date keywords
    lower_expr = expr.lower()
    if lower_expr.startswith("before "):
        date_val = expr[7:].strip()
        return str_value < date_val

    if lower_expr.startswith("after "):
        date_val = expr[6:].strip()
        return str_value > date_val

    # Exact match (case-insensitive for strings)
    return str_value.lower() == expr.lower()


def _apply_filters(
    data: list[dict[str, Any]], filters: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Apply filters to data rows.

    Supports Looker filter expressions including comparisons, wildcards,
    ranges, and null checks.

    Args:
        data: List of row dictionaries
        filters: Dict mapping field names to list of filter expressions

    Returns:
        Filtered list of rows
    """
    if not filters:
        return data

    filtered = []
    for row in data:
        matches = True
        for field, expressions in filters.items():
            if field not in row:
                matches = False
                break

            row_value = row[field]

            # Row matches if ANY of the filter expressions match (OR logic within field)
            field_matches = False
            for expr in expressions:
                if _parse_looker_filter(expr, row_value):
                    field_matches = True
                    break

            if not field_matches:
                matches = False
                break

        if matches:
            filtered.append(row)

    return filtered


def _apply_sorts(data: list[dict[str, Any]], sorts: list[str]) -> list[dict[str, Any]]:
    """Apply sorting to data rows.

    Args:
        data: List of row dictionaries
        sorts: List of sort specs. Supports:
            - '-field_name' for descending
            - 'field_name desc' for descending
            - 'field_name asc' or 'field_name' for ascending

    Returns:
        Sorted list of rows
    """
    if not sorts:
        return data

    result = data.copy()
    for sort_field in reversed(sorts):
        # Handle both formats: "-field_name" and "field_name desc/asc"
        sort_lower = sort_field.lower()
        if sort_field.startswith("-"):
            descending = True
            field_name = sort_field[1:]
        elif sort_lower.endswith(" desc"):
            descending = True
            field_name = sort_field[:-5].strip()
        elif sort_lower.endswith(" asc"):
            descending = False
            field_name = sort_field[:-4].strip()
        else:
            descending = False
            field_name = sort_field

        def sort_key(row: dict, fn: str = field_name) -> Any:
            val = row.get(fn, "")
            # Try numeric sort if possible
            try:
                return float(val)
            except (ValueError, TypeError):
                return str(val)

        result.sort(key=sort_key, reverse=descending)

    return result


def _apply_limit(data: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Apply row limit to data.

    Args:
        data: List of row dictionaries
        limit: Maximum number of rows to return

    Returns:
        Truncated list of rows
    """
    return data[:limit]


def _project_fields(data: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    """Project only requested fields from data rows.

    Args:
        data: List of row dictionaries
        fields: List of field names to include

    Returns:
        List of rows with only requested fields
    """
    return [{field: row.get(field, "") for field in fields} for row in data]


def _get_base_field_for_measure(measure_field: str, agg_type: str) -> str | None:
    """Extract the base field name from a derived measure field name.

    For auto-generated measures like 'view.field_sum' or 'view.field_average',
    extracts the base field 'view.field' that should be aggregated.

    Args:
        measure_field: Full measure field name (e.g., 'device_purchases.full_price_sum')
        agg_type: Aggregation type (e.g., 'sum', 'average', 'min', 'max')

    Returns:
        Base field name (e.g., 'device_purchases.full_price') or None if not a derived measure
    """
    # Aggregation suffixes that indicate derived measures
    agg_suffixes = {
        "sum": "_sum",
        "average": "_average",
        "avg": "_avg",
        "min": "_min",
        "max": "_max",
        "count_distinct": "_count_distinct",
        "sum_distinct": "_sum_distinct",
        "average_distinct": "_average_distinct",
        "median": "_median",
        "median_distinct": "_median_distinct",
    }

    suffix = agg_suffixes.get(agg_type)
    if suffix and measure_field.endswith(suffix):
        # Remove the suffix to get the base field
        return measure_field[: -len(suffix)]

    return None


def _apply_aggregation(
    data: list[dict[str, Any]],
    fields: list[str],
    measures: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply GROUP BY aggregation when measures are present.

    This implements Looker's semantic layer behavior: when a query includes
    both dimensions and measures, GROUP BY the dimensions and aggregate
    the measures.

    For derived measures (e.g., 'field_sum', 'field_average'), the base field
    is extracted and used for aggregation.

    Args:
        data: List of row dictionaries
        fields: All requested fields
        measures: Dict mapping measure field names to aggregation type
                  (e.g., {'service_requests.count': 'count'})

    Returns:
        Aggregated list of rows grouped by dimension fields
    """
    if not measures:
        # No measures - return data as-is
        return data

    # Separate dimensions from measures
    dimension_fields = [f for f in fields if f not in measures]

    # Group by dimensions
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in data:
        # Create group key from dimension values
        key = tuple(str(row.get(dim, "")) for dim in dimension_fields)
        if key not in groups:
            groups[key] = []
        groups[key].append(row)

    # Aggregate each group
    result = []
    for key, group_rows in groups.items():
        aggregated_row = {}

        # Copy dimension values
        for i, dim in enumerate(dimension_fields):
            aggregated_row[dim] = key[i]

        # Compute measure aggregates
        for measure_field, agg_type in measures.items():
            # For derived measures (e.g., full_price_sum), get the base field
            # to read values from (e.g., full_price)
            source_field = _get_base_field_for_measure(measure_field, agg_type)
            if source_field is None:
                # Not a derived measure, use the measure field directly
                source_field = measure_field

            if agg_type == "count":
                # Count aggregation - COUNT(*) in SQL
                # In Looker, type: count means count the number of rows in the group
                # This is equivalent to COUNT(*), not SUM(count_column)
                aggregated_row[measure_field] = len(group_rows)
            elif agg_type == "sum":
                # Sum aggregation
                total = 0.0
                for row in group_rows:
                    val = row.get(source_field, 0)
                    try:
                        total += float(val) if val else 0
                    except (ValueError, TypeError):
                        pass
                aggregated_row[measure_field] = total
            elif agg_type == "average" or agg_type == "avg":
                # Average aggregation
                values = []
                for row in group_rows:
                    val = row.get(source_field, None)
                    if val is not None:
                        try:
                            values.append(float(val))
                        except (ValueError, TypeError):
                            pass
                aggregated_row[measure_field] = sum(values) / len(values) if values else 0
            elif agg_type == "min":
                # Min aggregation
                values = []
                for row in group_rows:
                    val = row.get(source_field, None)
                    if val is not None:
                        try:
                            values.append(float(val))
                        except (ValueError, TypeError):
                            pass
                aggregated_row[measure_field] = min(values) if values else None
            elif agg_type == "max":
                # Max aggregation
                values = []
                for row in group_rows:
                    val = row.get(source_field, None)
                    if val is not None:
                        try:
                            values.append(float(val))
                        except (ValueError, TypeError):
                            pass
                aggregated_row[measure_field] = max(values) if values else None
            elif agg_type == "count_distinct":
                # Count distinct values - COUNT(DISTINCT column)
                unique_values = set()
                for row in group_rows:
                    val = row.get(source_field)
                    if val is not None and val != "":
                        unique_values.add(str(val))
                aggregated_row[measure_field] = len(unique_values)
            elif agg_type == "sum_distinct":
                # Sum of distinct values only
                unique_values = set()
                for row in group_rows:
                    val = row.get(source_field)
                    if val is not None and val != "":
                        try:
                            unique_values.add(float(val))
                        except (ValueError, TypeError):
                            pass
                aggregated_row[measure_field] = sum(unique_values)
            elif agg_type == "average_distinct":
                # Average of distinct values only
                unique_values = set()
                for row in group_rows:
                    val = row.get(source_field)
                    if val is not None and val != "":
                        try:
                            unique_values.add(float(val))
                        except (ValueError, TypeError):
                            pass
                aggregated_row[measure_field] = (
                    sum(unique_values) / len(unique_values) if unique_values else 0
                )
            elif agg_type == "median":
                # Median - midpoint value (50th percentile)
                values = []
                for row in group_rows:
                    val = row.get(source_field)
                    if val is not None and val != "":
                        try:
                            values.append(float(val))
                        except (ValueError, TypeError):
                            pass
                if values:
                    sorted_values = sorted(values)
                    n = len(sorted_values)
                    mid = n // 2
                    if n % 2 == 0:
                        aggregated_row[measure_field] = (
                            sorted_values[mid - 1] + sorted_values[mid]
                        ) / 2
                    else:
                        aggregated_row[measure_field] = sorted_values[mid]
                else:
                    aggregated_row[measure_field] = None
            elif agg_type == "median_distinct":
                # Median of distinct values only
                unique_values = set()
                for row in group_rows:
                    val = row.get(source_field)
                    if val is not None and val != "":
                        try:
                            unique_values.add(float(val))
                        except (ValueError, TypeError):
                            pass
                if unique_values:
                    sorted_values = sorted(unique_values)
                    n = len(sorted_values)
                    mid = n // 2
                    if n % 2 == 0:
                        aggregated_row[measure_field] = (
                            sorted_values[mid - 1] + sorted_values[mid]
                        ) / 2
                    else:
                        aggregated_row[measure_field] = sorted_values[mid]
                else:
                    aggregated_row[measure_field] = None
            elif agg_type == "list":
                # List - concatenated distinct values (like GROUP_CONCAT)
                unique_values = set()
                for row in group_rows:
                    val = row.get(source_field)
                    if val is not None and val != "":
                        unique_values.add(str(val))
                aggregated_row[measure_field] = ", ".join(sorted(unique_values))
            elif agg_type in ("number", "string", "date", "yesno"):
                # Non-aggregate types - just take the first value
                # These don't perform aggregation in Looker
                aggregated_row[measure_field] = (
                    group_rows[0].get(source_field) if group_rows else None
                )
            else:
                # Unknown aggregation type - default to count
                aggregated_row[measure_field] = len(group_rows)

        result.append(aggregated_row)

    return result


def get_query_data(
    fields: list[str],
    filters: dict[str, list[str]] | None = None,
    sorts: list[str] | None = None,
    limit: int = 5000,
    measures: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
    """Execute a query against DuckDB (primary) or CSV data (fallback).

    This function first attempts to execute the query directly on DuckDB,
    which is much more efficient for large datasets. If the table is not
    found in DuckDB, it falls back to loading CSV data into Python memory.

    When measures are specified, this performs GROUP BY aggregation like
    Looker's semantic layer: non-measure fields become dimensions that are
    grouped, and measures are aggregated within each group.

    Args:
        fields: List of fields to return (e.g., ['service_requests.agency'])
        filters: Dict mapping field names to acceptable values
        sorts: List of sort specs (prefix with '-' for descending)
        limit: Maximum rows to return
        measures: Dict mapping measure field names to aggregation type
                  (e.g., {'service_requests.count': 'count'})
                  If None, no aggregation is performed.

    Returns:
        List of result rows, or None if no data available for the view
    """
    view_name = _get_view_from_fields(fields)
    if not view_name:
        return None

    # Try DuckDB first (primary method - much more efficient)
    duckdb_result = _execute_duckdb_query(
        table_name=view_name,
        fields=fields,
        filters=filters,
        sorts=sorts,
        limit=limit,
        measures=measures,
    )
    if duckdb_result is not None:
        logger.debug(f"Query executed via DuckDB for {view_name}")
        return duckdb_result

    # Fall back to CSV loading (for backward compatibility)
    logger.debug(f"DuckDB query failed, falling back to CSV for {view_name}")
    if not has_seeded_data(view_name):
        return None

    # Load data from CSV
    data = load_seeded_data(view_name)

    # Apply filters BEFORE aggregation (like SQL WHERE clause)
    data = _apply_filters(data, filters or {})

    # Apply aggregation if measures are specified
    if measures:
        data = _apply_aggregation(data, fields, measures)

    # Apply sorts AFTER aggregation (like SQL ORDER BY)
    data = _apply_sorts(data, sorts or [])

    # Apply limit
    data = _apply_limit(data, limit)

    # Project fields (only if no aggregation was done - aggregation already projects)
    if not measures:
        data = _project_fields(data, fields)

    return data
