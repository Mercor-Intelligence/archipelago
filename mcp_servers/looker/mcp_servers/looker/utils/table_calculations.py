"""Table calculation expression validation and evaluation.

Supports a limited subset of Looker table calculation expressions using regex
pattern matching. This module handles validation and evaluation for both online
and offline modes.
"""

import re
from typing import Any

# Type alias for table calculation dict format
TableCalculationDict = dict[str, Any]

# Supported expression patterns
# Order matters: more specific patterns should come before general ones
SUPPORTED_PATTERNS = [
    (r"^row\(\)$", "row"),
    (r"^sum\(\s*\$\{([\w.]+)\}\s*\)$", "sum"),
    (r"^mean\(\s*\$\{([\w.]+)\}\s*\)$", "mean"),
    (r"^\$\{([\w.]+)\}\s*/\s*sum\(\s*\$\{([\w.]+)\}\s*\)$", "percent_of_total"),
    (r"^\$\{([\w.]+)\}\s*([+\-*/])\s*\$\{([\w.]+)\}$", "arithmetic"),
]


def validate_expression(expr: str) -> tuple[str, re.Match] | None:
    """Validate and parse a table calculation expression.

    Args:
        expr: The expression string to validate

    Returns:
        Tuple of (expression_type, match_object) if valid, None otherwise
    """
    expr = expr.strip()
    for pattern, expr_type in SUPPORTED_PATTERNS:
        if match := re.match(pattern, expr):
            return (expr_type, match)
    return None


def _to_numeric(value: Any) -> float:
    """Convert a value to numeric type for calculations.

    Args:
        value: Value to convert (int, float, str, or None)

    Returns:
        Numeric value (float), or 0.0 if conversion fails
    """
    if value is None:
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def eval_table_calc(
    expr: str, row_index: int, row_data: dict[str, Any], all_rows: list[dict[str, Any]]
) -> Any:
    """Evaluate a table calculation expression for a specific row.

    Args:
        expr: The expression string to evaluate
        row_index: 0-indexed row position
        row_data: Dictionary of field values for the current row
        all_rows: List of all row dictionaries (for aggregations)

    Returns:
        Calculated value for this row

    Raises:
        ValueError: If expression is unsupported or invalid
    """
    if not all_rows:
        raise ValueError("Cannot evaluate table calculation on empty result set")

    result = validate_expression(expr)
    if not result:
        raise ValueError(
            f"Unsupported table calculation: {expr}. "
            f"Supported: row(), sum(${{field}}), mean(${{field}}), "
            f"${{field}} / sum(${{field}}), basic arithmetic (+ - * /)"
        )

    expr_type, match = result

    if expr_type == "row":
        return row_index + 1  # 1-indexed row number

    if expr_type == "sum":
        field = match.group(1)
        return sum(_to_numeric(r.get(field)) for r in all_rows)

    if expr_type == "mean":
        field = match.group(1)
        values = [_to_numeric(r.get(field)) for r in all_rows]
        return sum(values) / len(values) if values else None

    if expr_type == "percent_of_total":
        field1, field2 = match.group(1), match.group(2)
        numerator = _to_numeric(row_data.get(field1))
        denominator = sum(_to_numeric(r.get(field2)) for r in all_rows)
        if denominator == 0:
            return None
        return numerator / denominator

    if expr_type == "arithmetic":
        field1, op, field2 = match.group(1), match.group(2), match.group(3)
        a = _to_numeric(row_data.get(field1))
        b = _to_numeric(row_data.get(field2))
        ops = {
            "+": lambda x, y: x + y,
            "-": lambda x, y: x - y,
            "*": lambda x, y: x * y,
            "/": lambda x, y: x / y if y != 0 else None,
        }
        return ops[op](a, b)

    raise ValueError(f"Unsupported expression type: {expr_type}")


def apply_table_calcs(
    rows: list[dict[str, Any]], dynamic_fields: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply table calculations to query result rows.

    Calculations are processed sequentially: each calculation is applied to all rows
    before moving to the next. This allows later calculations to reference previously
    calculated fields (e.g., ${rank} * ${orders.count}).

    Args:
        rows: List of row dictionaries (field name -> value)
        dynamic_fields: List of table calculation definitions, each with:
            - table_calculation: str - Field name for the calculated value
            - expression: str - Expression to evaluate
            - label: str (optional) - Display label
            - value_format_name: str (optional) - Format name (e.g., "percent_2", "decimal_0")

    Returns:
        List of row dictionaries with calculated fields added

    Raises:
        ValueError: If any table calculation expression is invalid
    """
    if not dynamic_fields:
        return rows

    if not rows:
        # Return empty list if no rows (table calculations require data)
        return rows

    # Start with copies of original rows
    result = [dict(row) for row in rows]

    # Track format specifications for each calculated field
    format_specs = {}

    # Process each calculation sequentially across all rows
    # This allows later calculations to reference previously calculated fields
    for calc in dynamic_fields:
        table_calc_name = calc.get("table_calculation")
        expression = calc.get("expression")
        if not table_calc_name or not expression:
            continue

        # Store format specification for later application
        format_specs[table_calc_name] = calc.get("value_format_name")

        # Apply this calculation to all rows (store raw values)
        for i, row_data in enumerate(result):
            try:
                # Use result (which includes previously calculated fields) for both
                # current row data and all_rows, so aggregations and references work correctly

                value = eval_table_calc(expression, i, row_data, result)
                row_data[table_calc_name] = value
            except ValueError as e:
                # Re-raise with context for better error messages
                raise ValueError(
                    f"Error evaluating table calculation '{table_calc_name}': {e}"
                ) from e

    # Apply formatting after all calculations are complete
    # This ensures calculations use raw values, but final output is formatted
    for row_data in result:
        for field_name, format_name in format_specs.items():
            if field_name in row_data and format_name:
                row_data[field_name] = format_value(row_data[field_name], format_name)

    return result


def dynamic_fields_to_dict(dynamic_fields: list[Any]) -> list[TableCalculationDict]:
    """Convert TableCalculation Pydantic models to dict format.

    This helper function reduces code duplication when converting TableCalculation
    objects to the dict format expected by apply_table_calcs().

    Args:
        dynamic_fields: List of TableCalculation objects (Pydantic models)

    Returns:
        List of dicts with table_calculation, expression, label, value_format_name keys
    """
    return [
        {
            "table_calculation": df.table_calculation,
            "expression": df.expression,
            "label": df.label,
            "value_format_name": df.value_format_name,
        }
        for df in dynamic_fields
    ]


def get_table_calculation_field_names(dynamic_fields: list[Any] | None) -> list[str]:
    """Extract table calculation field names from dynamic_fields.

    Args:
        dynamic_fields: List of TableCalculation objects or dicts, or None

    Returns:
        List of table_calculation field names (e.g., ["rank", "pct"])
    """
    if not dynamic_fields:
        return []

    field_names = []
    for df in dynamic_fields:
        # Handle both Pydantic models and dicts
        if hasattr(df, "table_calculation"):
            field_names.append(df.table_calculation)
        elif isinstance(df, dict) and "table_calculation" in df:
            field_names.append(df["table_calculation"])
    return field_names


def format_value(value: Any, format_name: str | None) -> Any:
    """Format a value according to the specified format name.

    Args:
        value: The value to format
        format_name: Format name (e.g., "percent_2", "decimal_0", "currency")

    Returns:
        Formatted value (may be string or number depending on format)
    """
    if format_name is None or value is None:
        return value

    # Handle percent formats (percent_0, percent_1, percent_2, etc.)
    if format_name.startswith("percent_"):
        try:
            decimals = int(format_name.split("_")[1])
            if isinstance(value, int | float):
                # Convert decimal to percentage (0.134 -> 13.4)
                return round(value * 100, decimals)
        except (ValueError, IndexError):
            # Invalid format name, return as-is
            pass

    # Handle decimal formats (decimal_0, decimal_1, decimal_2, etc.)
    if format_name.startswith("decimal_"):
        try:
            decimals = int(format_name.split("_")[1])
            if isinstance(value, int | float):
                return round(value, decimals)
        except (ValueError, IndexError):
            # Invalid format name, return as-is
            pass

    # Handle currency format
    if format_name == "currency":
        if isinstance(value, int | float):
            return round(value, 2)

    # Default: return as-is for unknown formats
    return value
