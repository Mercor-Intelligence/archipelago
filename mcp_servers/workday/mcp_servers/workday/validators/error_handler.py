"""Pydantic error handler that normalizes validation errors."""

from pydantic import ValidationError


def to_error_response(exc: ValidationError) -> dict:
    """Convert ValidationError to structured response with E_VAL_001."""
    return {
        "error": {
            "code": "E_VAL_001",
            "message": "Invalid input",
            "details": exc.errors(),
        }
    }
