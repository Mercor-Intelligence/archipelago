"""Shared middleware components for MCP servers."""

from .config import Configurator, apply_configurations
from .context import get_http_headers, set_http_headers
from .errors import (
    BadRequestError,
    ForbiddenError,
    InternalServerError,
    MCPError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
    error_handler,
    validate_email,
    validate_email_list,
    validate_required_fields,
)
from .injected_errors import (
    ErrorInjectionMiddleware,
    InjectedErrorRule,
    InjectedErrorsConfig,
    InjectedErrorType,
    setup_error_injection,
)
from .latency import LatencyMiddleware
from .logging import (
    LoggingConfigurator,
    LoggingMiddleware,
    clear_request_context,
    configure_logging,
    configure_logging_from_args,
    create_logging_middleware,
    get_logger,
    get_request_duration,
    log_activity,
    log_error,
    log_request,
    log_response,
    log_server_startup,
    set_request_context,
    setup_logging,
    start_request_timer,
)
from .ratelimit import Algorithm, RateLimitMiddleware
from .rest_bridge import RestBridgeMiddleware
from .version import __version__

__all__ = [
    "__version__",
    # Middleware
    "LatencyMiddleware",
    "LoggingMiddleware",
    "RateLimitMiddleware",
    "RestBridgeMiddleware",
    "Algorithm",
    # Configuration interface
    "Configurator",
    "LoggingConfigurator",
    "apply_configurations",
    # Context
    "get_http_headers",
    "set_http_headers",
    # Error handling
    "MCPError",
    "BadRequestError",
    "UnauthorizedError",
    "ForbiddenError",
    "NotFoundError",
    "ValidationError",
    "RateLimitError",
    "InternalServerError",
    "error_handler",
    "validate_required_fields",
    "validate_email",
    "validate_email_list",
    # Logging configuration (legacy - use LoggingConfigurator instead)
    "setup_logging",
    "configure_logging",
    "configure_logging_from_args",
    "create_logging_middleware",
    "get_logger",
    "set_request_context",
    "clear_request_context",
    "start_request_timer",
    "get_request_duration",
    "log_request",
    "log_response",
    "log_error",
    "log_activity",
    "log_server_startup",
    "ErrorInjectionMiddleware",
    "InjectedErrorRule",
    "InjectedErrorsConfig",
    "InjectedErrorType",
    "setup_error_injection",
]
