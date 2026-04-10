"""Context variables for MCP middleware."""

from contextvars import ContextVar

# ContextVar to store HTTP headers for authentication
# This allows RestBridgeMiddleware to pass headers to AuthGuard
# regardless of whether we're in stdio (with meta headers) or HTTP/SSE mode
http_headers_var: ContextVar[dict[str, str] | None] = ContextVar("http_headers", default=None)


def set_http_headers(headers: dict[str, str]) -> None:
    """Set HTTP headers in context.

    Args:
        headers: Dictionary of HTTP headers
    """
    http_headers_var.set(headers)


def get_http_headers() -> dict[str, str] | None:
    """Get HTTP headers from context.

    Returns:
        Dictionary of HTTP headers, or None if not set
    """
    return http_headers_var.get()
