"""Utility functions for Greenhouse MCP Server.

Contains helper functions for pagination, validation, and other common operations.
"""

# Import from shared mcp_middleware package
from mcp_middleware import (
    # Error handling
    BadRequestError,
    ForbiddenError,
    InternalServerError,
    MCPError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
    clear_request_context,
    configure_logging,
    error_handler,
    # Logging
    get_logger,
    get_request_duration,
    log_activity,
    log_error,
    log_request,
    log_response,
    set_request_context,
    start_request_timer,
    validate_email,
    validate_email_list,
    validate_required_fields,
)

# Backward compatibility alias
GreenhouseError = MCPError

__all__ = [
    # Error classes
    "GreenhouseError",
    "BadRequestError",
    "UnauthorizedError",
    "ForbiddenError",
    "NotFoundError",
    "ValidationError",
    "RateLimitError",
    "InternalServerError",
    # Error utilities
    "error_handler",
    "validate_required_fields",
    "validate_email",
    "validate_email_list",
    # Logging
    "configure_logging",
    "get_logger",
    "set_request_context",
    "clear_request_context",
    "start_request_timer",
    "get_request_duration",
    "log_request",
    "log_response",
    "log_error",
    "log_activity",
]
