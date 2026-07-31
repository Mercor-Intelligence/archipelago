"""Utility functions for data ingestion.

This module provides reusable utility functions that can be used across
different extractor implementations (XML, JSON, CSV, etc.).
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from .exceptions import ValidationError


def convert_type(value: Any, field_type: str, field_name: str) -> Any:
    """Convert a value to the specified type.

    This is a generic type conversion utility that can be used by any
    extractor implementation (XML, JSON, CSV, etc.) to ensure consistent
    type handling across all data sources.

    Args:
        value: The value to convert
        field_type: Target type ("string", "integer", "decimal", "date", "array")
        field_name: Name of the field being converted (for error messages)

    Returns:
        Converted value, or None if value is None/empty

    Raises:
        ValidationError: If type conversion fails

    Example:
        >>> convert_type("123", "integer", "age")
        123
        >>> convert_type("19.99", "decimal", "price")
        Decimal('19.99')
        >>> convert_type("invalid", "integer", "age")
        ValidationError: Type conversion failed for field 'age': cannot convert 'invalid' to integer
    """
    try:
        if field_type == "string":
            return str(value) if value is not None else None

        elif field_type == "integer":
            if value is None or value == "":
                return None
            # Reject booleans explicitly (bool is subclass of int in Python)
            if isinstance(value, bool):
                raise ValidationError(f"Field '{field_name}' expected integer, got boolean {value}")
            return int(value)

        elif field_type == "decimal":
            if value is None or value == "":
                return None
            return Decimal(str(value))

        elif field_type == "date":
            # Return as string - let application handle date parsing
            # (Different formats need different parsing logic)
            return str(value) if value is not None else None

        elif field_type == "array":
            # Array type should be handled by the extractor, not this utility
            raise ValidationError(
                f"Array type for field '{field_name}' should be handled by "
                "extractor, not convert_type"
            )

        else:
            # Should not reach here due to validation, but just in case
            raise ValidationError(f"Unknown type '{field_type}' for field '{field_name}'")

    except (ValueError, TypeError, InvalidOperation) as e:
        raise ValidationError(
            f"Type conversion failed for field '{field_name}': "
            f"cannot convert '{value}' to {field_type} - {e}"
        ) from e
