"""Utility helpers shared across USPTO MCP modules."""

from mcp_servers.uspto.utils.audit import log_audit_event
from mcp_servers.uspto.utils.errors import (
    AuthenticationError,
    CoverageError,
    DocumentsUnavailableError,
    DownloadUnavailableError,
    ForeignPriorityUnavailableError,
    InvalidQuerySyntaxError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    UpstreamAPIError,
    USPTOError,
    ValidationError,
    WorkspaceConflictError,
    format_pydantic_errors,
    handle_errors,
)
from mcp_servers.uspto.utils.logging import (
    REDACTED,
    configure_logging,
    generate_request_id,
    log_metric,
    redact_sensitive_data,
)


def normalize_log_level(level: str) -> str:
    """Normalize a log level string for the logger."""

    return level.upper()


def display_workspace_label(workspace_id: str) -> str:
    """Format a workspace label for logging and CLI output."""

    return f"workspace:{workspace_id}"


__all__ = [
    "AuthenticationError",
    "CoverageError",
    "DocumentsUnavailableError",
    "DownloadUnavailableError",
    "ForeignPriorityUnavailableError",
    "InvalidQuerySyntaxError",
    "InvalidRequestError",
    "NotFoundError",
    "REDACTED",
    "RateLimitError",
    "UpstreamAPIError",
    "USPTOError",
    "ValidationError",
    "WorkspaceConflictError",
    "configure_logging",
    "display_workspace_label",
    "format_pydantic_errors",
    "generate_request_id",
    "handle_errors",
    "log_audit_event",
    "log_metric",
    "normalize_log_level",
    "redact_sensitive_data",
]
