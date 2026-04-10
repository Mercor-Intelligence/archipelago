"""Global shared HTTP client for the Looker MCP server.

This module provides a singleton httpx.AsyncClient that is shared across
all repository instances. This prevents connection exhaustion that occurs
when each request creates its own client that never closes.

The client should be closed on server shutdown via close_http_client().

NOTE: This is purely an infrastructure optimization. It does not change
what tools can do or their behavior - only how HTTP connections are managed.
"""

import httpx
from config import settings

# Global shared client instance
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client.

    Uses settings.looker_verify_ssl to configure SSL verification.

    Returns:
        The shared AsyncClient instance for making HTTP requests.
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
            timeout=httpx.Timeout(120.0, connect=10.0),
            verify=settings.looker_verify_ssl,
        )
    return _http_client


async def close_http_client() -> None:
    """Close the shared HTTP client.

    Should be called on server shutdown to properly release connections.
    """
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def reset_http_client() -> None:
    """Reset the shared HTTP client without closing.

    This is primarily for testing, where event loops are created/destroyed
    between tests. Calling this allows a fresh client to be created for
    the next event loop.

    Note: This does NOT close the existing client. Use close_http_client()
    if you need to properly close connections.
    """
    global _http_client
    _http_client = None
