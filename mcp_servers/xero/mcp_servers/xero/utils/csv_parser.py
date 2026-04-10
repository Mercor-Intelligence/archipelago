"""CSV parsing utilities with dot notation support."""

import csv
import io
from typing import Any


def parse_csv_with_dot_notation(csv_content: str) -> list[dict]:
    """
    Parse CSV content with support for dot notation in headers.

    Converts flat CSV with dot notation headers into nested JSON structures.

    Examples:
        - "Contact.Name" → {"Contact": {"Name": "value"}}
        - "Addresses.0.City" → {"Addresses": [{"City": "value"}]}
        - "Phones.0.PhoneNumber" → {"Phones": [{"PhoneNumber": "value"}]}

    Args:
        csv_content: CSV string with headers

    Returns:
        List of dictionaries with nested structure

    Raises:
        ValueError: If CSV is malformed
    """
    if not csv_content or not csv_content.strip():
        raise ValueError("CSV content is empty")

    # Parse CSV
    reader = csv.DictReader(io.StringIO(csv_content))

    if not reader.fieldnames:
        raise ValueError("CSV has no headers")

    result = []

    for row in reader:
        # Skip empty rows
        if not any(row.values()):
            continue

        obj = {}

        for header, value in row.items():
            # Skip None or empty headers (trailing commas)
            if header is None or header == "":
                continue

            # Skip None or empty values
            if value is None or value == "":
                continue

            # Parse the header path
            set_nested_value(obj, header, value)

        result.append(obj)

    return result


def set_nested_value(obj: dict, path: str, value: str) -> None:
    """
    Set a value in a nested dictionary using dot notation.

    Args:
        obj: Dictionary to modify
        path: Dot-notation path (e.g., "Contact.Name" or "Addresses.0.City")
        value: Value to set
    """
    parts = path.split(".")
    current = obj

    i = 0
    while i < len(parts) - 1:
        part = parts[i]

        # Check if this part is an array index at root level
        if i == 0 and part.isdigit():
            raise ValueError(f"Invalid path: {path} - array index cannot be root")

        # Skip already processed array indices
        if part == "__skip__":
            i += 1
            continue

        # Check if next part is an array index
        next_part = parts[i + 1] if i + 1 < len(parts) else None
        is_next_array = next_part and next_part.isdigit()

        if is_next_array:
            # Ensure current[part] is a list
            if part not in current:
                current[part] = []
            elif not isinstance(current[part], list):
                raise ValueError(f"Expected list at {part} but found {type(current[part])}")

            # Ensure the list has enough elements
            index = int(next_part)
            while len(current[part]) <= index:
                current[part].append({})

            # Move into the array element
            current = current[part][index]
            # Skip the index part in next iteration
            i += 2  # Skip both current part and the index
        else:
            # Regular nested object
            if part not in current:
                current[part] = {}
            elif not isinstance(current[part], dict):
                # If it's already a list, we can't nest further
                if isinstance(current[part], list):
                    raise ValueError(f"Cannot set nested property on array at {part}")
                # Otherwise, overwrite
                current[part] = {}

            current = current[part]
            i += 1

    # Set the final value
    final_key = parts[-1]
    if final_key == "__skip__":
        return  # This was an array index, already handled

    # Type coercion for common types
    typed_value = coerce_value(value)
    current[final_key] = typed_value


def coerce_value(value: str) -> Any:
    """
    Coerce string value to appropriate Python type.

    Args:
        value: String value from CSV

    Returns:
        Coerced value (bool, int, float, or str)
    """
    # Boolean
    if value.lower() in ("true", "false"):
        return value.lower() == "true"

    # Integer
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        if value.startswith("0") and len(value) > 1 and not value.startswith("0."):
            return value
        return int(value)

    # Float
    try:
        if "." in value:
            return float(value)
    except ValueError:
        pass

    # String (default)
    return value


def merge_data(existing: list[dict], new: list[dict], id_field: str) -> tuple[list[dict], int, int]:
    """
    Merge new data with existing data, avoiding duplicates by ID.

    Args:
        existing: Existing list of items
        new: New list of items to merge
        id_field: Field name to use for identifying duplicates (e.g., "ContactID")

    Returns:
        Tuple of (merged list, rows_added, rows_updated)
    """
    # Create a mapping of existing items by ID
    existing_by_id = {item.get(id_field): item for item in existing if item.get(id_field)}

    rows_added = 0
    rows_updated = 0

    # Process new items
    for new_item in new:
        item_id = new_item.get(id_field)

        if not item_id:
            # No ID - just add it
            existing.append(new_item)
            rows_added += 1
        elif item_id in existing_by_id:
            # Update existing item (deep merge)
            existing_item = existing_by_id[item_id]
            deep_merge(existing_item, new_item)
            rows_updated += 1
        else:
            # New item with ID
            existing.append(new_item)
            existing_by_id[item_id] = new_item
            rows_added += 1

    return existing, rows_added, rows_updated


def deep_merge(target: dict, source: dict) -> None:
    """
    Deep merge source dict into target dict.

    Args:
        target: Target dictionary to merge into
        source: Source dictionary to merge from
    """
    for key, value in source.items():
        if key in target:
            if isinstance(target[key], dict) and isinstance(value, dict):
                # Recursively merge nested dicts
                deep_merge(target[key], value)
            elif isinstance(target[key], list) and isinstance(value, list):
                # For lists, extend (or you could replace - depends on use case)
                # For now, replace to avoid duplicates
                target[key] = value
            else:
                # Overwrite with new value
                target[key] = value
        else:
            # New key
            target[key] = value
