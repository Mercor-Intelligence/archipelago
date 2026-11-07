"""Utility functions for Tableau field checks."""

import re
from typing import Any


def find_by_id(data: list[dict[str, Any]], id_value: str) -> dict[str, Any] | None:
    """Find a record by its 'id' field."""
    for item in data:
        if str(item.get("id", "")) == str(id_value):
            return item
    return None


def find_by_name(data: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Find a record by its 'name' field (case-insensitive)."""
    name_lower = name.lower()
    for item in data:
        if str(item.get("name", "")).lower() == name_lower:
            return item
    return None


def find_by_field(
    data: list[dict[str, Any]], field: str, value: str
) -> dict[str, Any] | None:
    """Find a record by a specific field value (case-insensitive for strings)."""
    for item in data:
        item_value = item.get(field)
        if item_value is None:
            continue
        if str(item_value).lower() == str(value).lower():
            return item
    return None


def filter_by_field(
    data: list[dict[str, Any]], field: str, value: str
) -> list[dict[str, Any]]:
    """Filter records by a specific field value (case-insensitive for strings)."""
    result = []
    value_lower = str(value).lower()
    for item in data:
        item_value = item.get(field)
        if item_value is not None and str(item_value).lower() == value_lower:
            result.append(item)
    return result


def filter_by_site(data: list[dict[str, Any]], site_id: str) -> list[dict[str, Any]]:
    """Filter records by site_id."""
    return [item for item in data if str(item.get("site_id", "")) == str(site_id)]


def filter_by_pattern(
    data: list[dict[str, Any]], field: str, pattern: str
) -> list[dict[str, Any]]:
    """Filter records where field matches a regex pattern.

    Args:
        data: List of records to filter
        field: Field name to match against
        pattern: Regex pattern to match (e.g., "Dr.*_.*" for "Dr. Alice_Clinic")

    Returns:
        List of records matching the pattern

    Raises:
        ValueError: If pattern is not a valid regex
    """
    try:
        compiled_pattern = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}") from e

    result = []
    for item in data:
        item_value = item.get(field)
        if item_value is not None and compiled_pattern.search(str(item_value)):
            result.append(item)
    return result


def count_by_site_role(data: list[dict[str, Any]], site_role: str) -> int:
    """Count users by site_role (case-insensitive)."""
    site_role_lower = site_role.lower()
    return sum(
        1 for item in data if str(item.get("site_role", "")).lower() == site_role_lower
    )


def get_field_value(record: dict[str, Any] | None, field_name: str) -> Any:
    """Get a field value from a record, supporting nested fields with dot notation."""
    if record is None:
        return None

    # Support nested fields with dot notation (e.g., "site.name")
    if "." in field_name:
        parts = field_name.split(".")
        value = record
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    return record.get(field_name)
