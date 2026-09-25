"""
Middleware for logging tool requests and responses.

This middleware logs all tool calls and their results using loguru,
providing visibility into MCP server operations.
"""

from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger


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
        Process the request with logging.

        Args:
            context: The middleware context containing request information
            call_next: Callable to invoke the next middleware or handler

        Returns:
            The response from the downstream handler

        Raises:
            ValueError: If no fastmcp context is available
        """
        if not self.enabled:
            return await call_next(context)

        fastmcp_context = context.fastmcp_context
        if not fastmcp_context:
            logger.error("No fastmcp context")
            raise ValueError("LoggingMiddleware: No fastmcp context")

        response = await call_next(context)

        # Log based on configured log level
        log_method = getattr(logger, self.log_level.lower())

        if isinstance(response, ToolResult):
            log_method(f"{context.method} returned {response.content}")
        else:
            log_method(f"{context.method} returned {response}")

        return response

    def disable(self):
        """Disable the middleware (useful for testing)."""
        self.enabled = False

    def enable(self):
        """Enable the middleware."""
        self.enabled = True
