from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger

# Maximum characters for a single log message to prevent stderr stream overflow.
# When the platform reads stderr via process.stderr.readline(), lines exceeding
# the OS pipe buffer (~64KB) cause readline() to block indefinitely.
_MAX_LOG_LENGTH = 2000


def truncate_for_log(text: str, max_length: int = _MAX_LOG_LENGTH) -> str:
    """Truncate text to a safe length for logging to stderr.

    Prevents oversized log lines from blocking the platform's
    process.stderr.readline() call.
    """
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"... ({len(text)} chars total, truncated)"


class LoggingMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        fastmcp_context = context.fastmcp_context
        if not fastmcp_context:
            logger.error("No fastmcp context")
            raise ValueError("LoggingMiddleware: No fastmcp context")

        response = await call_next(context)
        # Use lazy=True so the expensive str() + truncate conversions
        # only execute when DEBUG is actually enabled — avoids wasted
        # CPU when the logger is configured for WARNING+ in production.
        logger.opt(lazy=True).debug(
            "{method} returned {result}",
            method=lambda: context.method,
            result=lambda: truncate_for_log(
                str(response.content) if isinstance(response, ToolResult) else str(response)
            ),
        )
        return response
