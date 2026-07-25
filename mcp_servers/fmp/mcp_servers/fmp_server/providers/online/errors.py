"""Error types and handling for online provider.

This module defines error codes and helper functions for generating
standardized error responses in online mode.
"""

from enum import Enum
from typing import Any


class OnlineErrorCode(str, Enum):
    """Error codes for online mode failures."""

    INVALID_API_KEY = "INVALID_API_KEY"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    API_ERROR = "API_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"


def make_error_response(
    code: OnlineErrorCode,
    message: str,
    suggestion: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Create a standardized error response.

    Args:
        code: The error code from OnlineErrorCode enum
        message: Human-readable error message
        suggestion: Optional actionable guidance for the user
        **extra: Additional fields to include in the response

    Returns:
        A dictionary with error information structured consistently.
    """
    error: dict[str, Any] = {
        "code": code.value,
        "message": message,
    }
    if suggestion:
        error["suggestion"] = suggestion

    return {
        "data": None,
        "error": error,
        **extra,
    }


def invalid_api_key() -> dict[str, Any]:
    """Create error response for invalid API key.

    Returns:
        Standardized error response for invalid API key.
    """
    return make_error_response(
        code=OnlineErrorCode.INVALID_API_KEY,
        message="Invalid or missing API key",
        suggestion="Set the FMP_API_KEY environment variable with a valid API key.",
    )


def rate_limited(retry_after: int | None = None) -> dict[str, Any]:
    """Create error response for rate limiting.

    Args:
        retry_after: Optional seconds to wait before retrying

    Returns:
        Standardized error response for rate limiting.
    """
    suggestion = "Wait before making more requests."
    if retry_after is not None:
        suggestion = f"Wait {retry_after} seconds before retrying."

    return make_error_response(
        code=OnlineErrorCode.RATE_LIMITED,
        message="API rate limit exceeded",
        suggestion=suggestion,
    )


def network_error(error_msg: str) -> dict[str, Any]:
    """Create error response for network errors.

    Args:
        error_msg: The original error message

    Returns:
        Standardized error response for network errors.
    """
    return make_error_response(
        code=OnlineErrorCode.NETWORK_ERROR,
        message=f"Network error: {error_msg}",
        suggestion="Check your internet connection and try again.",
    )


def api_error(status_code: int, error_msg: str) -> dict[str, Any]:
    """Create error response for API errors.

    Args:
        status_code: The HTTP status code
        error_msg: The error message from the API

    Returns:
        Standardized error response for API errors.
    """
    return make_error_response(
        code=OnlineErrorCode.API_ERROR,
        message=f"API error (HTTP {status_code}): {error_msg}",
    )


def validation_error(error_msg: str) -> dict[str, Any]:
    """Create error response for validation errors.

    Args:
        error_msg: The validation error message

    Returns:
        Standardized error response for validation errors.
    """
    return make_error_response(
        code=OnlineErrorCode.VALIDATION_ERROR,
        message=f"Validation error: {error_msg}",
    )
