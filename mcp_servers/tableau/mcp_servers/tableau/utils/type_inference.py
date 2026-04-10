"""Type inference utilities for Tableau view metadata.

Provides functions to infer Tableau-compatible field data types and roles
by inspecting actual data values. Mimics Tableau Metadata API behavior
for offline mode.

Reference: https://help.tableau.com/current/api/metadata_api/en-us/reference/
"""

import re
from typing import Any

from models import FieldDataType, FieldRole, TableauFieldMetadata

# Regex patterns for date/datetime detection
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")

# Field name patterns that indicate DIMENSION even for numeric types
DIMENSION_NAME_PATTERNS = [
    r"_id$",
    r"^id$",
    r"_key$",
    r"_code$",
    r"^code$",
    r"_num$",
    r"_number$",
    r"^zip",
    r"^postal",
    r"^phone",
    r"^fax",
]


def infer_field_data_type(values: list[Any]) -> FieldDataType:
    """Infer Tableau-compatible data type from a list of values.

    Inspects non-null values to determine the most appropriate type.
    Type detection priority:
    1. BOOLEAN - if all values are True/False
    2. INTEGER - if all numeric values are whole numbers
    3. REAL - if any numeric values have decimals
    4. DATETIME - if string values match ISO datetime format
    5. DATE - if string values match ISO date format
    6. STRING - default fallback

    Args:
        values: List of sample values from a field

    Returns:
        Inferred FieldDataType enum value
    """
    # Filter out None values
    non_null_values = [v for v in values if v is not None]

    if not non_null_values:
        return FieldDataType.UNKNOWN

    # Check for boolean
    if all(isinstance(v, bool) for v in non_null_values):
        return FieldDataType.BOOLEAN

    # Check for numeric types (must check before string since numbers could be strings)
    numeric_values = []
    has_float = False

    for v in non_null_values:
        if isinstance(v, bool):
            # Python bool is subclass of int, skip it
            continue
        if isinstance(v, int):
            numeric_values.append(v)
        elif isinstance(v, float):
            numeric_values.append(v)
            if v != int(v):  # Has decimal component
                has_float = True
        elif isinstance(v, str):
            # Try to parse as number
            try:
                parsed = float(v)
                numeric_values.append(parsed)
                if "." in v and parsed != int(parsed):
                    has_float = True
            except (ValueError, TypeError):
                pass

    # If all non-null values are numeric
    if len(numeric_values) == len(non_null_values):
        return FieldDataType.REAL if has_float else FieldDataType.INTEGER

    # Check for date/datetime strings
    string_values = [v for v in non_null_values if isinstance(v, str)]

    if string_values and len(string_values) == len(non_null_values):
        # Check datetime first (more specific)
        if all(DATETIME_PATTERN.match(v) for v in string_values):
            return FieldDataType.DATETIME
        # Check date
        if all(DATE_PATTERN.match(v) for v in string_values):
            return FieldDataType.DATE

    # Default to STRING
    return FieldDataType.STRING


def infer_field_role(name: str, data_type: FieldDataType) -> FieldRole:
    """Infer Tableau field role (DIMENSION or MEASURE) based on name and type.

    Heuristics:
    - MEASURE: Numeric types (INTEGER, REAL) by default
    - DIMENSION: STRING, DATE, DATETIME, BOOLEAN
    - Exception: Fields with ID/key/code patterns are DIMENSION even if numeric

    Args:
        name: Field name
        data_type: Inferred data type

    Returns:
        FieldRole enum value
    """
    if data_type == FieldDataType.UNKNOWN:
        return FieldRole.UNKNOWN

    # Check if field name suggests DIMENSION (IDs, codes, keys)
    name_lower = name.lower()
    for pattern in DIMENSION_NAME_PATTERNS:
        if re.search(pattern, name_lower):
            return FieldRole.DIMENSION

    # Numeric types are MEASURE by default
    if data_type in (FieldDataType.INTEGER, FieldDataType.REAL):
        return FieldRole.MEASURE

    # Everything else is DIMENSION
    return FieldRole.DIMENSION


def extract_field_metadata(
    data: list[dict],
    include_sample_values: bool = True,
    sample_limit: int = 5,
) -> list[TableauFieldMetadata]:
    """Extract field metadata from a list of data rows.

    Analyzes the data to determine column names, types, roles, and
    collects sample values.

    Args:
        data: List of row dictionaries from sample_data_json
        include_sample_values: Whether to include sample values
        sample_limit: Maximum number of sample values per field

    Returns:
        List of TableauFieldMetadata objects
    """
    if not data:
        return []

    # Get all field names from the first row (assume consistent schema)
    field_names = list(data[0].keys())
    result = []

    for field_name in field_names:
        # Collect all values for this field
        all_values = [row.get(field_name) for row in data]

        # Check for nullability
        has_null = any(v is None for v in all_values)

        # Infer data type
        data_type = infer_field_data_type(all_values)

        # Infer role
        role = infer_field_role(field_name, data_type)

        # Collect sample values (unique, non-null, up to limit)
        sample_values: list[Any] = []
        if include_sample_values:
            seen = set()
            for v in all_values:
                if v is not None and len(sample_values) < sample_limit:
                    # Use string representation for set membership (handles unhashable types)
                    v_str = str(v)
                    if v_str not in seen:
                        seen.add(v_str)
                        sample_values.append(v)

        result.append(
            TableauFieldMetadata(
                name=field_name,
                data_type=data_type,
                role=role,
                nullable=has_null,
                sample_values=sample_values,
            )
        )

    return result
