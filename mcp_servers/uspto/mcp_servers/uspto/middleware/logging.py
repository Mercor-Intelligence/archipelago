"""Logging middleware for the USPTO MCP Server."""

from __future__ import annotations

import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger

from mcp_servers.uspto.utils.logging import generate_request_id, redact_sensitive_data


def _redact_response_content(content: Any) -> Any:
    """Redact sensitive data from response content."""
    if isinstance(content, dict):
        return redact_sensitive_data(content)
    elif isinstance(content, list):
        return [_redact_response_content(item) for item in content]
    return content


def _extract_tool_params(arguments: dict[str, Any]) -> dict[str, Any]:
    """Extract sanitized tool parameters for logging.

    Returns a subset of parameters suitable for logging,
    excluding large payloads and sensitive data.
    """
    params = {}
    for key, value in arguments.items():
        # Skip large or complex objects
        if isinstance(value, bytes | bytearray):
            params[key] = f"<binary:{len(value)}bytes>"
        elif isinstance(value, str) and len(value) > 200:
            params[key] = f"{value[:200]}...<truncated>"
        elif isinstance(value, list | tuple) and len(value) > 10:
            params[key] = f"<{type(value).__name__}:{len(value)}items>"
        else:
            params[key] = value
    return redact_sensitive_data(params)


def _determine_error_code(exception: Exception) -> str:
    """Map exception to standardized error code.

    For USPTOError and its subclasses, uses the exception's code attribute
    to preserve specific error codes like "OFFLINE_MODE" or "UPSTREAM_API_ERROR".
    For other exceptions, falls back to class name mapping.
    """
    # Check if exception has a code attribute (USPTOError and subclasses)
    if hasattr(exception, "code"):
        code = exception.code
        # Validate that code is a non-empty string to ensure type safety
        if isinstance(code, str) and code:
            return code
        # If code is None or non-string, convert to string or fall through to class name mapping
        if code is not None:
            return str(code)

    # Fall back to class name mapping for standard exceptions
    error_name = type(exception).__name__
    error_code_mapping = {
        "AuthenticationError": "AUTH_FAILED",
        "RateLimitError": "RATE_LIMIT_EXCEEDED",
        "UpstreamAPIError": "UPSTREAM_API_ERROR",
        "ValidationError": "VALIDATION_ERROR",
        "TimeoutError": "TIMEOUT",
        "ConnectionError": "CONNECTION_ERROR",
        "ValueError": "INVALID_INPUT",
        "KeyError": "MISSING_FIELD",
        "FileNotFoundError": "RESOURCE_NOT_FOUND",
    }

    return error_code_mapping.get(error_name, error_name.upper())


class LoggingMiddleware(Middleware):
    """Middleware that logs all tool invocations with comprehensive observability.

    Provides:
    - Request/response logging with request IDs
    - Performance metrics (execution time, cache hits)
    - Error tracking with standardized codes
    - Upstream API call counting
    - PII protection (no user_id, no API keys)
    """

    def __init__(self) -> None:
        """Initialize logging middleware."""
        super().__init__()
        # Metrics tracking (per-session, in-memory)
        self._request_count: dict[str, int] = {}
        self._cache_hits: dict[str, int] = {}
        self._cache_misses: dict[str, int] = {}
        self._error_count: dict[str, int] = {}

    async def on_request(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        """Handle request logging with comprehensive observability."""
        fastmcp_context = context.fastmcp_context
        if not fastmcp_context:
            logger.error("No fastmcp context")
            raise ValueError("LoggingMiddleware: No fastmcp context")

        request_id = generate_request_id()
        method = context.method
        start_time = time.time()

        # Extract and sanitize parameters
        arguments: dict[str, Any] = {}
        if hasattr(context, "arguments") and context.arguments:
            arguments = _extract_tool_params(dict(context.arguments))

        # Track request count
        self._request_count[method] = self._request_count.get(method, 0) + 1

        # Request logging with structured fields
        logger.info(
            "Tool invoked",
            extra={
                "request_id": request_id,
                "tool": method,
                "params": arguments,
            },
        )

        cache_hit = False
        upstream_calls = 0

        try:
            response = await call_next(context)
            execution_time_ms = round((time.time() - start_time) * 1000, 2)

            # Detect cache hits from response metadata
            if isinstance(response, ToolResult) and isinstance(response.content, dict):
                content = response.content
                # Check for cached response indicators
                # Cache hit only when metadata exists but execution_time_ms is None
                metadata = content.get("metadata")
                if isinstance(metadata, dict):
                    if metadata.get("execution_time_ms") is None:
                        cache_hit = True
                        self._cache_hits[method] = self._cache_hits.get(method, 0) + 1
                    else:
                        self._cache_misses[method] = self._cache_misses.get(method, 0) + 1

                    # Count upstream API calls (if present in response)
                    upstream_calls = metadata.get("upstream_calls", 0)
                else:
                    # Metadata is missing or not a dict, cannot determine cache status
                    upstream_calls = 0

            # Performance logging with metrics
            logger.info(
                "Tool completed",
                extra={
                    "request_id": request_id,
                    "tool": method,
                    "execution_time_ms": execution_time_ms,
                    "cache_hit": cache_hit,
                    "upstream_calls": upstream_calls,
                },
            )

            # Debug-level response logging (redacted)
            if isinstance(response, ToolResult):
                redacted_content = _redact_response_content(response.content)
                logger.debug(
                    f"{method} response",
                    extra={
                        "request_id": request_id,
                        "response": redacted_content,
                    },
                )
            else:
                redacted_response = _redact_response_content(response)
                logger.debug(
                    f"{method} response",
                    extra={
                        "request_id": request_id,
                        "response": redacted_response,
                    },
                )

            return response

        except Exception as e:
            execution_time_ms = round((time.time() - start_time) * 1000, 2)
            error_code = _determine_error_code(e)

            # Track error count
            self._error_count[error_code] = self._error_count.get(error_code, 0) + 1

            # Error logging with structured fields
            logger.error(
                "Tool failed",
                extra={
                    "request_id": request_id,
                    "tool": method,
                    "error_code": error_code,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "execution_time_ms": execution_time_ms,
                    "error_details": {
                        "exception_class": f"{type(e).__module__}.{type(e).__name__}",
                        "params": arguments,
                    },
                },
            )
            raise

    def get_metrics(self) -> dict[str, Any]:
        """Get current session metrics.

        Returns:
            Dictionary containing:
            - request_count: Requests per tool
            - cache_hit_rate: Cache effectiveness per tool
            - error_count: Errors by error code
        """
        metrics: dict[str, Any] = {
            "request_count": dict(self._request_count),
            "cache_hit_rate": {},
            "error_count": dict(self._error_count),
        }

        # Calculate cache hit rates
        for tool in set(list(self._cache_hits.keys()) + list(self._cache_misses.keys())):
            hits = self._cache_hits.get(tool, 0)
            misses = self._cache_misses.get(tool, 0)
            total = hits + misses
            if total > 0:
                metrics["cache_hit_rate"][tool] = round(hits / total * 100, 2)

        return metrics
