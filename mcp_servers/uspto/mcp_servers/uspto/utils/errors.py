"""Custom exception hierarchy and error handling for the USPTO MCP Server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from loguru import logger
from pydantic import ValidationError as PydanticValidationError

from mcp_servers.uspto.utils.logging import redact_sensitive_data


class USPTOError(Exception):
    """Base exception for USPTO MCP Server."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int = 500,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code
        super().__init__(message)

    def to_response(self) -> dict[str, Any]:
        """Convert exception to error response format."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class AuthenticationError(USPTOError):
    """Authentication failed (401/403)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            details=details,
            status_code=401 if "MISSING" in code else 403,
        )


class InvalidRequestError(USPTOError):
    """Malformed request or missing required fields (400)."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="INVALID_REQUEST",
            message=message,
            details=details,
            status_code=400,
        )


class ValidationError(USPTOError):
    """Request validation failed (422)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            details=details,
            status_code=422,
        )


class RateLimitError(USPTOError):
    """Rate limit exceeded (429)."""

    def __init__(
        self,
        limit: int,
        retry_after: int,
        reset_at: int | None = None,
    ) -> None:
        from datetime import UTC, datetime

        details: dict[str, Any] = {"limit": limit, "retryAfter": retry_after}
        if reset_at is not None:
            reset_iso = datetime.fromtimestamp(reset_at, tz=UTC).isoformat()
            details["resetAt"] = reset_iso

        super().__init__(
            code="RATE_LIMIT_EXCEEDED",
            message=(
                f"Rate limit exceeded: {limit} requests/minute allowed. "
                f"Retry after {retry_after} seconds."
            ),
            details=details,
            status_code=429,
        )


class NotFoundError(USPTOError):
    """Resource not found (404)."""

    def __init__(
        self,
        resource_type: str,
        resource_id: str,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        # Generate descriptive, actionable message
        if message is None:
            resource_label = resource_type.replace("_", " ").title()
            message = (
                f"{resource_label} {resource_id} does not exist "
                "or is not accessible in the current session"
            )

        error_details = {"resourceType": resource_type, "resourceId": resource_id}
        if details:
            error_details.update(details)

        super().__init__(
            code=f"{resource_type.upper()}_NOT_FOUND",
            message=message,
            details=error_details,
            status_code=404,
        )


class CoverageError(USPTOError):
    """Data outside USPTO dataset coverage (422)."""

    # Default coverage dates for USPTO Open Data Portal
    DEFAULT_COVERAGE_START = "2001-01-01"
    DEFAULT_COVERAGE_END = "present"

    def __init__(
        self,
        application_number: str,
        reason: str,
        suggestion: str | None = None,
        coverage_details: dict[str, Any] | None = None,
    ) -> None:
        # Start with coverage_details, then set required fields to prevent overwrite
        details: dict[str, Any] = dict(coverage_details) if coverage_details else {}
        details["applicationNumber"] = application_number
        details["reason"] = reason

        # Add coverage dates if not provided
        if "coverageStart" not in details:
            details["coverageStart"] = self.DEFAULT_COVERAGE_START
        if "coverageEnd" not in details:
            details["coverageEnd"] = self.DEFAULT_COVERAGE_END

        # Generate suggestion if not provided
        if suggestion is None:
            suggestion = (
                f"Only applications filed after {details['coverageStart']} are available. "
                "Try searching with a more recent application number."
            )
        details["suggestion"] = suggestion

        # Build actionable message with coverage dates
        message = (
            f"Application {application_number} is outside dataset coverage. "
            f"{reason}. Coverage period: {details['coverageStart']} to {details['coverageEnd']}."
        )

        super().__init__(
            code="DATASET_COVERAGE_UNAVAILABLE",
            message=message,
            details=details,
            status_code=422,
        )


class UpstreamAPIError(USPTOError):
    """USPTO API returned error (502/503)."""

    def __init__(
        self,
        upstream_status_code: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="UPSTREAM_API_ERROR",
            message=f"USPTO API error: {message}",
            details={
                "upstreamStatusCode": upstream_status_code,
                **(details or {}),
            },
            status_code=503,
        )


class OfflineModeError(USPTOError):
    """Server is in offline mode and cannot access live USPTO API (503)."""

    def __init__(
        self,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        error_details: dict[str, Any] = {
            "offlineMode": True,
            "instruction": "Restart the server with --online flag to enable live USPTO API access",
        }
        if operation:
            error_details["operation"] = operation
        if details:
            error_details.update(details)

        message = (
            "USPTO API is running in offline mode. Live data is not available. "
            "Restart with --online flag to enable live API access."
        )

        super().__init__(
            code="OFFLINE_MODE",
            message=message,
            details=error_details,
            status_code=503,
        )


class WorkspaceConflictError(USPTOError):
    """Workspace name conflict (409)."""

    def __init__(self, workspace_name: str) -> None:
        super().__init__(
            code="WORKSPACE_NAME_CONFLICT",
            message=f"Workspace with name '{workspace_name}' already exists",
            details={"workspaceName": workspace_name},
            status_code=409,
        )


class QueryConflictError(USPTOError):
    """Query name conflict within workspace (409)."""

    def __init__(
        self, query_name: str, workspace_id: str, existing_query_id: str | None = None
    ) -> None:
        details: dict[str, Any] = {"queryName": query_name, "workspaceId": workspace_id}
        if existing_query_id:
            details["existingQueryId"] = existing_query_id
        super().__init__(
            code="QUERY_NAME_CONFLICT",
            message=f"A query with name '{query_name}' already exists in this workspace",
            details=details,
            status_code=409,
        )


class SnapshotConflictError(USPTOError):
    """Snapshot version conflict within workspace (409)."""

    def __init__(self, application_number: str, version: int, workspace_id: str) -> None:
        details: dict[str, Any] = {
            "applicationNumber": application_number,
            "version": version,
            "workspaceId": workspace_id,
        }
        super().__init__(
            code="SNAPSHOT_VERSION_CONFLICT",
            message=(
                f"Snapshot version {version} for application '{application_number}' "
                f"already exists in this workspace"
            ),
            details=details,
            status_code=409,
        )


class InvalidQuerySyntaxError(USPTOError):
    """USPTO query syntax error (422)."""

    def __init__(self, query: str, parse_error: str) -> None:
        super().__init__(
            code="INVALID_QUERY_SYNTAX",
            message=f"Invalid USPTO query syntax: {parse_error}",
            details={"query": query, "parseError": parse_error},
            status_code=422,
        )


class DocumentsUnavailableError(USPTOError):
    """Documents not available for application (422)."""

    def __init__(self, application_number: str, reason: str | None = None) -> None:
        super().__init__(
            code="DOCUMENTS_UNAVAILABLE",
            message=f"Document metadata not available for application {application_number}",
            details={
                "applicationNumber": application_number,
                **({"reason": reason} if reason else {}),
            },
            status_code=422,
        )


class DownloadUnavailableError(USPTOError):
    """Download URL not available (422)."""

    def __init__(self, document_id: str, reason: str | None = None) -> None:
        super().__init__(
            code="DOWNLOAD_UNAVAILABLE",
            message=f"No download options available for document {document_id}",
            details={
                "documentIdentifier": document_id,
                **({"reason": reason} if reason else {}),
            },
            status_code=422,
        )


class ForeignPriorityUnavailableError(USPTOError):
    """Foreign priority not available (422)."""

    def __init__(self, application_number: str, reason: str | None = None) -> None:
        super().__init__(
            code="FOREIGN_PRIORITY_UNAVAILABLE",
            message=f"Foreign priority data not available for {application_number}",
            details={
                "applicationNumber": application_number,
                **({"reason": reason} if reason else {}),
            },
            status_code=422,
        )


def format_pydantic_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """Format Pydantic validation errors for user-friendly responses.

    Converts Pydantic error format to actionable field-level messages.
    """
    formatted = []
    for error in errors:
        field = ".".join(str(loc) for loc in error.get("loc", []))
        error_type = error.get("type", "unknown")
        raw_msg = error.get("msg", "Validation error")

        # Generate actionable message based on error type
        message = _get_actionable_error_message(field, error_type, raw_msg, error)

        formatted.append(
            {
                "field": field,
                "message": message,
                "type": error_type,
            }
        )
    return formatted


def _get_actionable_error_message(
    field: str, error_type: str, raw_msg: str, error: dict[str, Any]
) -> str:
    """Generate actionable error message based on error type."""
    ctx = error.get("ctx", {})

    # Pattern matching for common validation types
    if error_type == "string_pattern_mismatch":
        pattern = ctx.get("pattern", "")
        if "ws_" in pattern:
            return f"{field} must be a valid workspace ID (format: ws_xxxxxxxxxxxx)"
        if "query_" in pattern:
            return f"{field} must be a valid query ID (format: query_xxxxxxxxxxxx)"
        if "snap_" in pattern:
            return f"{field} must be a valid snapshot ID (format: snap_xxxxxxxxxxxx)"
        return f"{field} must match the required format"

    if error_type == "string_too_short":
        min_len = ctx.get("min_length", 1)
        return f"{field} must be at least {min_len} character(s)"

    if error_type == "string_too_long":
        max_len = ctx.get("max_length", "N/A")
        return f"{field} must be at most {max_len} character(s)"

    if error_type == "missing":
        return f"{field} is required"

    if error_type == "int_parsing" or error_type == "int_type":
        return f"{field} must be a valid integer"

    if error_type == "greater_than":
        limit = ctx.get("gt")
        if limit is None:
            limit = 0
        return f"{field} must be greater than {limit}"

    if error_type == "greater_than_equal":
        limit = ctx.get("ge")
        if limit is None:
            limit = 0
        return f"{field} must be greater than or equal to {limit}"

    if error_type == "less_than":
        limit = ctx.get("lt")
        if limit is None:
            limit = "N/A"
        return f"{field} must be less than {limit}"

    if error_type == "less_than_equal":
        limit = ctx.get("le")
        if limit is None:
            limit = "N/A"
        return f"{field} must be less than or equal to {limit}"

    if error_type == "enum":
        expected = ctx.get("expected", "valid values")
        return f"{field} must be one of: {expected}"

    if error_type == "url_parsing":
        return f"{field} must be a valid URL"

    if error_type == "datetime_parsing":
        return f"{field} must be a valid ISO 8601 datetime (e.g., 2025-01-15T10:30:00Z)"

    # Default: use the raw message but make it field-specific
    return f"{field}: {raw_msg}"


def handle_errors[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """
    Decorator to catch and format all tool errors.

    Apply this decorator to all MCP tool functions to ensure consistent
    error handling and standardized error responses.

    Usage:
        @mcp.tool()
        @handle_errors
        async def uspto_applications_search(request: SearchRequest):
            ...

    Error Handling:
        - USPTOError: Re-raised as-is with logging
        - PydanticValidationError: Converted to ValidationError (422)
        - Other exceptions: Converted to USPTOError with INTERNAL_SERVER_ERROR (500)
    """

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)

        except USPTOError as e:
            # Redact sensitive data from error details before logging
            redacted_details = redact_sensitive_data(e.details) if e.details else {}
            logger.error(
                f"Tool error: {e.code}",
                error_code=e.code,
                details=redacted_details,
            )
            raise

        except PydanticValidationError as e:
            # Don't log str(e) - it includes input_value which may contain sensitive data
            error_count = len(e.errors())
            fields = [".".join(str(loc) for loc in err.get("loc", [])) for err in e.errors()]
            logger.error(
                f"Validation error: {error_count} field(s) failed",
                error_count=error_count,
                fields=fields,
            )
            formatted_errors = format_pydantic_errors(e.errors())

            # Build a user-friendly summary message
            if error_count == 1:
                summary = formatted_errors[0]["message"]
            else:
                summary = f"{error_count} validation errors: " + ", ".join(
                    err["field"] for err in formatted_errors[:3]
                )
                if error_count > 3:
                    summary += f" and {error_count - 3} more"

            raise ValidationError(
                code="VALIDATION_ERROR",
                message=summary,
                details={"errors": formatted_errors},
            ) from e

        except Exception as e:
            logger.exception("Unexpected error in tool execution")
            raise USPTOError(
                code="INTERNAL_SERVER_ERROR",
                message="An unexpected error occurred",
                details={"errorType": type(e).__name__},
                status_code=500,
            ) from e

    return wrapper


__all__ = [
    "AuthenticationError",
    "CoverageError",
    "DocumentsUnavailableError",
    "DownloadUnavailableError",
    "ForeignPriorityUnavailableError",
    "InvalidQuerySyntaxError",
    "InvalidRequestError",
    "NotFoundError",
    "OfflineModeError",
    "QueryConflictError",
    "RateLimitError",
    "SnapshotConflictError",
    "UpstreamAPIError",
    "USPTOError",
    "ValidationError",
    "WorkspaceConflictError",
    "format_pydantic_errors",
    "handle_errors",
]
