"""
Middleware and configuration for logging in MCP servers.

This module provides:
- LoggingMiddleware: Middleware for logging tool requests and responses
- Centralized logging configuration with loguru
- Context tracking (request_id, tool_name, persona, duration)
- Structured JSON output for production
- Pretty console output for development
- LoggingConfigurator: Composable configuration for MCP servers

Usage in main.py:
    from mcp_middleware import LoggingConfigurator, apply_configurations

    parser = argparse.ArgumentParser()
    configurators = [LoggingConfigurator()]
    args, remaining = apply_configurations(parser, mcp, configurators)
"""

import argparse
import contextvars
import os
import sys
import time
import uuid
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger

# Context variables for request tracking
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
tool_name_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_name", default=None
)
persona_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("persona", default=None)
start_time_var: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "start_time", default=None
)


def _get_persona_from_auth() -> str | None:
    """Try to get persona from mcp_auth if available.

    This provides optional integration with mcp_auth package. If mcp_auth is
    installed and a user is authenticated, returns the first role as persona.

    Returns:
        Persona string (first role) or None if not available
    """
    try:
        from mcp_auth import get_current_user

        user = get_current_user()
        if user:
            # Return first role as persona (e.g., "recruiter", "coordinator")
            roles = user.get("roles", [])
            if roles:
                return roles[0]
            # Fallback to username if no roles
            return user.get("username")
    except ImportError:
        # mcp_auth not installed, skip
        pass
    except Exception:
        # Any other error, skip silently
        pass
    return None


class LoggingMiddleware(Middleware):
    """
    Middleware that logs tool requests and responses.

    Args:
        enabled (bool): Whether middleware is active (default: True)
        log_level (str): Log level to use for messages (default: "DEBUG")
    """

    def __init__(self, enabled: bool = True, log_level: str = "DEBUG"):
        """
        Initialize the LoggingMiddleware.

        Args:
            enabled: Whether to enable logging
            log_level: The log level to use ("DEBUG", "INFO", "WARNING", "ERROR")

        Raises:
            ValueError: If log_level is not valid
        """
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if log_level.upper() not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")

        self.enabled = enabled
        self.log_level = log_level.upper()

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        """
        Process requests with logging.

        Args:
            context: The middleware context containing request information
            call_next: Callable to invoke the next middleware or handler

        Returns:
            The response from the downstream handler
        """
        if not self.enabled:
            return await call_next(context)

        # Generate request ID and extract context
        request_id = str(uuid.uuid4())[:8]
        tool_name = getattr(context.message, "name", None) or context.method or "unknown"

        # Set request context
        set_request_context(request_id=request_id, tool_name=tool_name, persona=None)
        start_request_timer()

        # Get bound logger with request context
        log = get_logger()
        log_method = getattr(log, self.log_level.lower())
        log_method(f"Calling {tool_name}")

        try:
            # Call the next middleware/handler
            response = await call_next(context)

            # Try to get persona after auth middleware has run
            persona = _get_persona_from_auth()
            set_request_context(request_id=request_id, tool_name=tool_name, persona=persona)

            # Get request duration
            duration_ms = get_request_duration()

            # Log the response using configured level
            if isinstance(response, ToolResult):
                result_str = str(response.content)
            else:
                result_str = str(response)

            # Truncate large responses for logging (first 500 chars)
            if len(result_str) > 500:
                result_str = result_str[:500] + "..."

            # Log completion with duration and output preview
            log = get_logger()  # Re-bind with updated persona
            log_method = getattr(log, self.log_level.lower())
            if duration_ms is not None:
                log_method(f"{tool_name} completed in {duration_ms:.0f}ms: {result_str}")
            else:
                log_method(f"{tool_name} completed: {result_str}")

            return response

        except Exception as e:
            # Try to get persona from auth context
            persona = _get_persona_from_auth()
            set_request_context(request_id=request_id, tool_name=tool_name, persona=persona)

            # Get request duration
            duration_ms = get_request_duration()

            # Log the error (always use error level for exceptions)
            log_error(tool_name, e, duration_ms)

            # Re-raise the exception
            raise

        finally:
            # Always clear request context
            clear_request_context()

    def disable(self):
        """Disable the middleware (useful for testing)."""
        self.enabled = False

    def enable(self):
        """Enable the middleware."""
        self.enabled = True


class LoggingConfigurator:
    """Configurator for logging setup.

    Adds --log-level and --json-logs arguments and configures
    both loguru and LoggingMiddleware.
    """

    def setup(self, parser: argparse.ArgumentParser) -> None:
        """Add logging arguments to the parser.

        Args:
            parser: ArgumentParser to add arguments to
        """
        parser.add_argument(
            "--log-level",
            type=str,
            default=os.environ.get("LOG_LEVEL", "INFO"),
            choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            help="Set the logging level (default: INFO, env: LOG_LEVEL)",
        )
        parser.add_argument(
            "--json-logs",
            action="store_true",
            default=os.environ.get("JSON_LOGS", "").lower() in ("1", "true", "yes"),
            help="Enable structured JSON logging for production (env: JSON_LOGS=1)",
        )
        parser.add_argument(
            "--log-file",
            type=str,
            default=os.environ.get("LOG_FILE"),
            help="Write logs to file (in addition to stderr) (env: LOG_FILE)",
        )

    def configure(
        self,
        mcp: FastMCP,
        log_level: str,
        json_logs: bool,
        log_file: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Configure logging based on parsed arguments.

        Args:
            mcp: FastMCP server instance
            log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            json_logs: Enable structured JSON logging for production
            log_file: Optional file path to write logs to
            **kwargs: Additional parsed arguments
        """
        # Configure loguru
        configure_logging(log_level=log_level, json_logs=json_logs, log_file=log_file)

        # Add middleware with the same log level
        mcp.add_middleware(create_logging_middleware(log_level=log_level))

        # Log startup with comprehensive server info
        log_server_startup(mcp, log_level, json_logs, log_file)


def setup_logging(parser: argparse.ArgumentParser) -> None:
    """Add logging arguments to an argument parser.

    Args:
        parser: ArgumentParser to add logging arguments to
    """
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO, env: LOG_LEVEL)",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        default=os.environ.get("JSON_LOGS", "").lower() in ("1", "true", "yes"),
        help="Enable structured JSON logging for production (env: JSON_LOGS=1)",
    )


def configure_logging(
    log_level: str = "INFO", json_logs: bool = False, log_file: str | None = None
) -> None:
    """Configure Loguru logger for the application.

    Args:
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_logs: If True, output structured JSON logs (for production)
                  If False, output pretty console logs (for development)
        log_file: Optional file path to write logs to (always uses JSON format)
    """
    from pathlib import Path

    # Remove default logger
    logger.remove()

    if json_logs:
        # Production: Structured JSON logs using custom sink
        # We use a sink instead of format to avoid Loguru treating JSON braces as placeholders
        def json_sink(message):
            record = message.record
            log_entry = _json_formatter(record)
            sys.stderr.write(log_entry)
            sys.stderr.flush()

        logger.add(
            json_sink,
            level=log_level.upper(),
            colorize=False,
            backtrace=True,
            diagnose=False,  # Don't include variable values in production
        )
    else:
        # Development: Pretty console logs with colors
        # Use a custom filter to add default values for missing context fields
        def add_defaults(record):
            record["extra"].setdefault("tool_name", "-")
            record["extra"].setdefault("request_id", "-")
            record["extra"].setdefault("persona", "-")
            return True

        logger.add(
            sys.stderr,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{extra[tool_name]}</cyan> | "
                "<yellow>{extra[request_id]}</yellow> | "
                "{extra[persona]} | "
                "<level>{message}</level>"
            ),
            level=log_level.upper(),
            colorize=True,
            backtrace=True,
            diagnose=True,
            filter=add_defaults,
        )

    # Add file handler if log_file is specified
    if log_file:
        # Ensure parent directory exists
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # File logs always use JSON format
        # Use Loguru's built-in serialize=True for automatic JSON serialization
        logger.add(
            log_file,
            serialize=True,
            level=log_level.upper(),
            colorize=False,
            backtrace=True,
            diagnose=False,
            rotation="10 MB",  # Rotate when file reaches 10MB
            retention="7 days",  # Keep logs for 7 days
            compression="gz",  # Compress rotated logs
        )


def configure_logging_from_args(args: argparse.Namespace) -> None:
    """Configure logging from parsed command-line arguments.

    Args:
        args: Parsed arguments from argparse (must have log_level and json_logs)
    """
    configure_logging(log_level=args.log_level, json_logs=args.json_logs)


def create_logging_middleware(log_level: str = "INFO") -> LoggingMiddleware:
    """Create a LoggingMiddleware instance with the specified log level.

    Args:
        log_level: Log level for the middleware (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Returns:
        Configured LoggingMiddleware instance
    """
    return LoggingMiddleware(enabled=True, log_level=log_level)


def _json_formatter(record: dict[str, Any]) -> str:
    """Format log record as JSON for structured logging.

    Args:
        record: Loguru record dictionary

    Returns:
        JSON-formatted log string
    """
    import json
    import traceback as tb_module
    from datetime import UTC, datetime

    # Build structured log entry
    log_entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "tool_name": record["extra"].get("tool_name"),
        "request_id": record["extra"].get("request_id"),
        "persona": record["extra"].get("persona"),
        "duration_ms": record["extra"].get("duration_ms"),
    }

    # Add exception info if present
    if record["exception"]:
        # Format traceback as string (JSON serializable)
        tb_lines = tb_module.format_tb(record["exception"].traceback)
        log_entry["exception"] = {
            "type": record["exception"].type.__name__,
            "value": str(record["exception"].value),
            "traceback": "".join(tb_lines),
        }

    # Add file/function/line info
    log_entry["source"] = {
        "file": record["file"].name,
        "function": record["function"],
        "line": record["line"],
    }

    return json.dumps(log_entry) + "\n"


def get_logger() -> Any:
    """Get the configured logger with current context.

    Returns:
        Logger instance bound with current request context
    """
    return logger.bind(
        request_id=request_id_var.get() or "-",
        tool_name=tool_name_var.get() or "-",
        persona=persona_var.get() or "-",
        duration_ms=None,
    )


def set_request_context(
    request_id: str | None = None,
    tool_name: str | None = None,
    persona: str | None = None,
) -> None:
    """Set request context for logging.

    Args:
        request_id: Unique identifier for the request
        tool_name: Name of the MCP tool being called
        persona: User persona (hr_admin, manager, employee, etc.)
    """
    if request_id:
        request_id_var.set(request_id)
    if tool_name:
        tool_name_var.set(tool_name)
    if persona:
        persona_var.set(persona)


def clear_request_context() -> None:
    """Clear all request context variables."""
    request_id_var.set(None)
    tool_name_var.set(None)
    persona_var.set(None)
    start_time_var.set(None)


def start_request_timer() -> None:
    """Start timing a request for performance monitoring."""
    start_time_var.set(time.time())


def get_request_duration() -> float | None:
    """Get duration of current request in milliseconds.

    Returns:
        Duration in milliseconds, or None if timer not started
    """
    start_time = start_time_var.get()
    if start_time:
        return (time.time() - start_time) * 1000
    return None


def log_request(tool_name: str, params: dict[str, Any]) -> None:
    """Log an incoming tool request.

    Args:
        tool_name: Name of the tool being called
        params: Tool parameters
    """
    log = get_logger()
    log.info(f"Request: {tool_name}", params=params)


def log_response(tool_name: str, result: Any, duration_ms: float | None = None) -> None:
    """Log a tool response.

    Args:
        tool_name: Name of the tool
        result: Tool result
        duration_ms: Request duration in milliseconds
    """
    log = get_logger()
    extra = {}
    if duration_ms is not None:
        extra["duration_ms"] = round(duration_ms, 2)

    # Truncate large responses for logging
    result_str = str(result)
    if len(result_str) > 500:
        result_str = result_str[:500] + "... (truncated)"

    extra["result"] = result_str
    log.info(f"Response: {tool_name}", **extra)


def log_error(tool_name: str, error: Exception, duration_ms: float | None = None) -> None:
    """Log a tool error.

    Args:
        tool_name: Name of the tool
        error: The exception that occurred
        duration_ms: Request duration in milliseconds
    """
    log = get_logger()
    extra = {}
    if duration_ms is not None:
        extra["duration_ms"] = round(duration_ms, 2)

    # Don't use f-string with loguru - pass values as arguments for proper formatting
    log.error("Error in {}: {!r}", tool_name, error, **extra)


def log_activity(
    action: str,
    persona: str | None,
    **kwargs: Any,
) -> None:
    """Log an activity for the activity feed.

    Args:
        action: What changed (e.g., "candidate_created", "application_advanced")
        persona: Who made the change
        **kwargs: Additional context (e.g., candidate_id, application_id, details)
    """
    log = get_logger()
    extra = {
        "action": action,
        "persona": persona or "system",
    }
    # Merge any additional kwargs into extra
    extra.update(kwargs)
    log.info(f"Activity: {action}", **extra)


def log_server_startup(
    mcp: FastMCP, log_level: str, json_logs: bool, log_file: str | None = None
) -> None:
    """Log comprehensive server startup information.

    Args:
        mcp: FastMCP server instance
        log_level: Configured log level
        json_logs: Whether JSON logging is enabled
        log_file: Optional file path where logs are being written
    """
    import platform

    # Extract server info
    server_name = getattr(mcp, "name", "MCP Server")
    server_version = getattr(mcp, "version", "unknown")
    server_description = getattr(mcp, "instructions", "")

    # Create a bound logger with startup context
    startup_logger = logger.bind(
        tool_name="startup",
        request_id="-",
        persona="system",
        duration_ms=None,
    )

    # Log startup banner
    startup_logger.info("=" * 70)
    startup_logger.info(f"🚀 Starting {server_name} v{server_version}")
    if server_description:
        startup_logger.info(f"   {server_description}")
    startup_logger.info("=" * 70)

    # Log configuration
    startup_logger.info("Configuration:")
    startup_logger.info(f"  • Log Level: {log_level}")
    log_format = "JSON (production)" if json_logs else "Pretty Console (development)"
    startup_logger.info(f"  • Log Format: {log_format}")
    if log_file:
        startup_logger.info(f"  • Log File: {os.path.abspath(log_file)}")

    # Log environment info
    startup_logger.info("Environment:")
    startup_logger.info(f"  • Python: {platform.python_version()}")
    startup_logger.info(f"  • Platform: {platform.system()} {platform.release()}")
    startup_logger.info(f"  • Working Directory: {os.getcwd()}")

    # Log middleware status
    middleware_count = len(getattr(mcp, "middleware", []))
    startup_logger.info(f"Middleware: {middleware_count} handler(s) registered")

    startup_logger.info("=" * 70)
    startup_logger.info(f"✅ {server_name} is ready to accept requests")
    startup_logger.info("=" * 70)
