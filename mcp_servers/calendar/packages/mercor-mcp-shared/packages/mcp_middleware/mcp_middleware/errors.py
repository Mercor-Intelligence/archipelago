"""Error handling utilities for MCP servers.

Provides:
- Standard HTTP error response format
- HTTP status code mapping
- Error handler decorator for tools
- Custom exception classes
- Validation utilities
"""

import functools
from typing import Any, ParamSpec, TypeVar

from .logging import get_logger, get_request_duration, log_error

_P = ParamSpec("_P")
_R = TypeVar("_R")


class MCPError(Exception):
    """Base exception for MCP server errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        errors: list[dict[str, str]] | None = None,
    ):
        """Initialize MCP error.

        Args:
            message: Human-readable error message
            status_code: HTTP status code
            errors: List of field-specific errors
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.errors = errors or []

    def to_dict(self) -> dict[str, Any]:
        """Convert error to API response format.

        Returns:
            Error response dictionary
        """
        result: dict[str, Any] = {"message": self.message}
        if self.errors:
            result["errors"] = self.errors
        return result


class BadRequestError(MCPError):
    """400 Bad Request - Malformed request, missing required fields."""

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None):
        super().__init__(message, status_code=400, errors=errors)


class UnauthorizedError(MCPError):
    """401 Unauthorized - Missing or invalid auth token."""

    def __init__(self, message: str = "Authentication required"):
        super().__init__(message, status_code=401)


class ForbiddenError(MCPError):
    """403 Forbidden - Persona lacks permission."""

    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message, status_code=403)


class NotFoundError(MCPError):
    """404 Not Found - Resource not found."""

    def __init__(self, resource: str, resource_id: int | str):
        message = f"{resource} with id {resource_id} not found"
        super().__init__(message, status_code=404)


class ValidationError(MCPError):
    """422 Unprocessable Entity - Validation error, invalid state transition."""

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None):
        super().__init__(message, status_code=422, errors=errors)


class RateLimitError(MCPError):
    """429 Too Many Requests - Rate limit exceeded."""

    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, status_code=429)


class InternalServerError(MCPError):
    """500 Internal Server Error - Unexpected errors."""

    def __init__(self, message: str = "Internal server error"):
        super().__init__(message, status_code=500)


def validate_required_fields(data: dict[str, Any], required_fields: list[str]) -> None:
    """Validate that required fields are present in data.

    Args:
        data: Data dictionary to validate
        required_fields: List of required field names

    Raises:
        BadRequestError: If any required fields are missing
    """
    missing_fields = [
        field for field in required_fields if field not in data or data[field] is None
    ]

    if missing_fields:
        errors = [
            {"field": field, "message": f"Field '{field}' is required"} for field in missing_fields
        ]
        raise BadRequestError("Missing required fields", errors=errors)


def validate_email(email: str | None) -> None:
    """Validate email format.

    Args:
        email: Email address to validate

    Raises:
        ValidationError: If email format is invalid
    """
    if not email:
        return

    # Split into local and domain parts
    parts = email.split("@")
    if len(parts) != 2:
        raise ValidationError(
            "Invalid email format",
            errors=[{"field": "email", "message": "Must be a valid email address"}],
        )

    local_part, domain = parts

    # Check local part is not empty
    if not local_part:
        raise ValidationError(
            "Invalid email format",
            errors=[{"field": "email", "message": "Must be a valid email address"}],
        )

    # Check domain has at least one dot with content on both sides
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValidationError(
            "Invalid email format",
            errors=[{"field": "email", "message": "Must be a valid email address"}],
        )


def validate_email_list(emails: list[dict[str, Any]] | None) -> None:
    """Validate list of email addresses.

    Args:
        emails: List of email dictionaries with 'value' and 'type' keys

    Raises:
        ValidationError: If email list is invalid
    """
    if not emails:
        raise ValidationError(
            "At least one email address is required",
            errors=[
                {
                    "field": "email_addresses",
                    "message": "At least one email address is required",
                }
            ],
        )

    for i, email_obj in enumerate(emails):
        if not isinstance(email_obj, dict):
            raise ValidationError(
                "Invalid email format",
                errors=[{"field": f"email_addresses[{i}]", "message": "Must be an object"}],
            )

        if "value" not in email_obj or not email_obj["value"]:
            raise ValidationError(
                "Invalid email format",
                errors=[
                    {
                        "field": f"email_addresses[{i}].value",
                        "message": "Email value is required",
                    }
                ],
            )

        validate_email(email_obj["value"])


def error_handler(func):
    """Decorator to handle errors in tool functions.

    Catches exceptions and formats them according to standard API error format.
    Also logs errors with request duration.

    Usage:
        @error_handler
        async def my_tool(...):
            # Tool implementation
    """
    import inspect

    # Create appropriate wrapper based on whether func is async or sync
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: _P.args, **kwargs: _P.kwargs):
            tool_name = func.__name__
            log = get_logger()

            try:
                result = await func(*args, **kwargs)
                return result

            except MCPError as e:
                # Known MCP errors - already formatted
                duration_ms = get_request_duration()
                log_error(tool_name, e, duration_ms)
                return {
                    "error": True,
                    "status_code": e.status_code,
                    **e.to_dict(),
                }

            except ValueError as e:
                # Value errors become validation errors
                duration_ms = get_request_duration()
                error = ValidationError(str(e))
                log_error(tool_name, error, duration_ms)
                return {
                    "error": True,
                    "status_code": 422,
                    **error.to_dict(),
                }

            except KeyError as e:
                # Key errors become bad request errors
                duration_ms = get_request_duration()
                error = BadRequestError(f"Missing required field: {e!s}")
                log_error(tool_name, error, duration_ms)
                return {
                    "error": True,
                    "status_code": 400,
                    **error.to_dict(),
                }

            except Exception as e:
                # Unexpected errors become internal server errors
                duration_ms = get_request_duration()
                log.exception(f"Unexpected error in {tool_name}")
                error = InternalServerError(f"An unexpected error occurred: {e!s}")
                return {
                    "error": True,
                    "status_code": 500,
                    **error.to_dict(),
                }

        return async_wrapper
    else:

        @functools.wraps(func)
        def sync_wrapper(*args: _P.args, **kwargs: _P.kwargs):
            tool_name = func.__name__
            log = get_logger()

            try:
                result = func(*args, **kwargs)
                return result

            except MCPError as e:
                # Known MCP errors - already formatted
                duration_ms = get_request_duration()
                log_error(tool_name, e, duration_ms)
                return {
                    "error": True,
                    "status_code": e.status_code,
                    **e.to_dict(),
                }

            except ValueError as e:
                # Value errors become validation errors
                duration_ms = get_request_duration()
                error = ValidationError(str(e))
                log_error(tool_name, error, duration_ms)
                return {
                    "error": True,
                    "status_code": 422,
                    **error.to_dict(),
                }

            except KeyError as e:
                # Key errors become bad request errors
                duration_ms = get_request_duration()
                error = BadRequestError(f"Missing required field: {e!s}")
                log_error(tool_name, error, duration_ms)
                return {
                    "error": True,
                    "status_code": 400,
                    **error.to_dict(),
                }

            except Exception as e:
                # Unexpected errors become internal server errors
                duration_ms = get_request_duration()
                log.exception(f"Unexpected error in {tool_name}")
                error = InternalServerError(f"An unexpected error occurred: {e!s}")
                return {
                    "error": True,
                    "status_code": 500,
                    **error.to_dict(),
                }

        return sync_wrapper
