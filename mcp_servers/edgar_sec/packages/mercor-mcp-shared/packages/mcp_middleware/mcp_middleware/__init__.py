"""Shared middleware components for MCP servers."""

from .config import Configurator, apply_configurations
from .context import get_http_headers, set_http_headers
from .db_tools import (
    ClearDatabaseRequest,
    ClearDatabaseResponse,
    CSVExportRequest,
    CSVExportResponse,
    CSVImportRequest,
    CSVImportResponse,
    ListTablesRequest,
    ListTablesResponse,
    create_database_tools,
    get_db_management_tools,
)
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
from .fs_tools import (
    DownloadFileRequest,
    DownloadFileResponse,
    FileInfo,
    FsRoot,
    ListFolderRequest,
    ListFolderResponse,
    ListFsRootsRequest,
    ListFsRootsResponse,
    create_filesystem_tools,
    get_filesystem_tools,
    register_fs_roots,
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
from .response_limiter import ResponseLimiterMiddleware
from .rest_bridge import RestBridgeMiddleware
from .runner import ServerConfig, get_server_config, get_server_directory, run_server
from .schema_utils import apply_default_setup
from .server_info import (
    ServerInfoInput,
    ServerInfoResponse,
    register_server_info_tool,
)
from .validation_error_sanitizer import (
    ValidationErrorSanitizerMiddleware,
    format_validation_error,
)
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
    # Server runner
    "ServerConfig",
    "get_server_config",
    "get_server_directory",
    "run_server",
    # Server info tool
    "ServerInfoInput",
    "ServerInfoResponse",
    "register_server_info_tool",
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
    # Database tools
    "create_database_tools",
    "get_db_management_tools",
    "CSVExportRequest",
    "CSVExportResponse",
    "CSVImportRequest",
    "CSVImportResponse",
    "ClearDatabaseRequest",
    "ClearDatabaseResponse",
    "ListTablesRequest",
    "ListTablesResponse",
    # Filesystem tools
    "create_filesystem_tools",
    "get_filesystem_tools",
    "register_fs_roots",
    "DownloadFileRequest",
    "DownloadFileResponse",
    "FileInfo",
    "FsRoot",
    "ListFolderRequest",
    "ListFolderResponse",
    "ListFsRootsRequest",
    "ListFsRootsResponse",
    # Response limiter / pagination
    "ResponseLimiterMiddleware",
    # Validation error sanitizer
    "ValidationErrorSanitizerMiddleware",
    "format_validation_error",
    # Schema utilities
    "apply_default_setup",
]
