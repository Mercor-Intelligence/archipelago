"""Context variables for MCP middleware.

Re-exports http_headers functions from mcp_auth for backwards compatibility.
The canonical source is now mcp_auth.context to avoid circular imports.
"""

from mcp_auth import get_http_headers, http_headers_var, set_http_headers

__all__ = [
    "get_http_headers",
    "http_headers_var",
    "set_http_headers",
]
