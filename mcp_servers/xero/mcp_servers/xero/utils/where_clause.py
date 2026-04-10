"""Shared utilities for validating and parsing Xero-style where clauses.

This module provides validation for where clause expressions used in Xero API
filtering. Where clauses support equality comparisons with strings, numbers,
and booleans, as well as AND conditions.

Examples:
    - Field=="value"
    - Field==123
    - Field==true
    - Field1=="value1" AND Field2=="value2"
    - FromBankAccount.AccountID=="uuid"
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def validate_where_clause(where: str) -> None:
    """Validate that a where clause is properly formed.

    Args:
        where: The where clause to validate

    Raises:
        ValueError: If the where clause is malformed
    """
    # Handle AND conditions - validate each condition separately
    if " AND " in where:
        conditions = where.split(" AND ")
        for condition in conditions:
            _validate_single_condition(condition.strip())
        return

    _validate_single_condition(where)


def _validate_single_condition(condition: str) -> None:
    """Validate a single where condition.

    Args:
        condition: A single condition like Field=="value"

    Raises:
        ValueError: If the condition is malformed
    """
    # Reject unsupported OR operator
    if " OR " in condition:
        raise ValueError(
            f"Invalid where clause: '{condition}'. "
            "OR operator is not supported. Use multiple queries instead."
        )

    # Must contain == operator (we only support equality)
    if "==" not in condition:
        raise ValueError(
            f"Invalid where clause: '{condition}'. "
            "Where clause must contain a comparison operator (==)."
        )

    # Check for unquoted non-numeric values
    parts = condition.split("==", 1)
    if len(parts) == 2:
        value = parts[1].strip()

        # Check for properly quoted strings - must be a single quoted value
        # not something like "value1" OR Field2=="value2"
        is_double_quoted = value.startswith('"') and value.endswith('"')
        is_single_quoted = value.startswith("'") and value.endswith("'")

        if is_double_quoted:
            # Ensure there's only one pair of double quotes (no embedded OR)
            inner = value[1:-1]
            if '"' in inner:
                raise ValueError(
                    f"Invalid where clause: '{condition}'. "
                    "Malformed value - check for unsupported operators or unescaped quotes."
                )
        elif is_single_quoted:
            # Ensure there's only one pair of single quotes
            inner = value[1:-1]
            if "'" in inner:
                raise ValueError(
                    f"Invalid where clause: '{condition}'. "
                    "Malformed value - check for unsupported operators or unescaped quotes."
                )

        is_quoted = is_double_quoted or is_single_quoted
        is_numeric = _is_numeric(value)
        is_boolean = value.lower() in ("true", "false")

        if not is_quoted and not is_numeric and not is_boolean:
            raise ValueError(
                f"Invalid where clause: '{condition}'. "
                f'String values must be quoted (e.g., Field=="value").'
            )


def _is_numeric(value: str) -> bool:
    """Check if a string value is numeric."""
    try:
        float(value)
        return True
    except ValueError:
        return False


def apply_where_filter(items: list[dict], where: str) -> list[dict]:
    """Apply basic where clause filtering to a list of dictionaries.

    Supports simple equality checks like:
    - BankTransferID=="uuid"
    - FromIsReconciled==true
    - Amount==5000.0
    - Field1=="value1" AND Field2=="value2"
    - Nested fields: FromBankAccount.AccountID=="uuid"

    Args:
        items: List of dictionaries to filter
        where: Where clause expression

    Returns:
        Filtered list of dictionaries

    Raises:
        ValueError: If the where clause cannot be applied
    """
    if "==" not in where:
        return items

    try:
        # Handle AND operator by splitting and applying filters sequentially
        if " AND " in where:
            conditions = where.split(" AND ")
            for condition in conditions:
                items = _apply_single_condition(items, condition.strip())
            return items
        else:
            return _apply_single_condition(items, where)

    except Exception as e:
        logger.error(f"Failed to apply where clause '{where}': {e}")
        raise ValueError(f"Failed to apply where filter: {e}") from e


def _apply_single_condition(items: list[dict], condition: str) -> list[dict]:
    """Apply a single where condition.

    Args:
        items: List of dictionaries
        condition: Single condition like BankTransferID=="uuid"

    Returns:
        Filtered items
    """
    if "==" not in condition:
        return items

    parts = condition.split("==", 1)
    if len(parts) != 2:
        return items

    field = parts[0].strip()
    raw_value = parts[1].strip()

    # Parse the value - remove quotes and handle type conversion
    value: Any
    if (raw_value.startswith('"') and raw_value.endswith('"')) or (
        raw_value.startswith("'") and raw_value.endswith("'")
    ):
        value = raw_value[1:-1]  # String value
    elif raw_value.lower() == "true":
        value = True
    elif raw_value.lower() == "false":
        value = False
    elif _is_numeric(raw_value):
        value = float(raw_value)
    else:
        value = raw_value

    # Handle nested fields (e.g., FromBankAccount.AccountID)
    if "." in field:
        field_parts = field.split(".", 1)
        if len(field_parts) == 2:
            parent, child = field_parts
            return [
                item
                for item in items
                if parent in item
                and isinstance(item[parent], dict)
                and _compare_values(item[parent].get(child), value)
            ]
    else:
        # Simple field comparison
        return [item for item in items if _compare_values(item.get(field), value)]

    return items


def _compare_values(actual: Any, expected: Any) -> bool:
    """Compare two values for equality, handling type coercion.

    Args:
        actual: The actual value from the data
        expected: The expected value from the filter

    Returns:
        True if values are equal
    """
    if actual is None:
        return expected is None

    # Handle numeric comparison
    if isinstance(expected, float):
        try:
            return float(actual) == expected
        except (ValueError, TypeError):
            return False

    # Handle boolean comparison
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual == expected
        if isinstance(actual, str):
            return actual.lower() == str(expected).lower()
        return bool(actual) == expected

    # String comparison
    return str(actual) == str(expected)
