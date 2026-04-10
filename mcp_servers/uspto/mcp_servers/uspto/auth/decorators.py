"""Authentication and rate limiting decorators for USPTO MCP tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any

from mcp_servers.uspto.auth.keys import APIKeyManager
from mcp_servers.uspto.auth.rate_limiter import (
    RATE_LIMIT_STATE_KEY,
    RateLimitInfo,
    rate_limiter,
)
from mcp_servers.uspto.utils.errors import AuthenticationError, RateLimitError

# Task-local storage for rate limit info (safe for concurrent async)
_current_rate_limit_info: ContextVar[RateLimitInfo | None] = ContextVar(
    "rate_limit_info", default=None
)


def require_api_key[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """
    Decorator to require and validate API key for MCP tool.

    Usage:
        @mcp.tool()
        @require_api_key
        async def uspto_applications_search(request: SearchRequest):
            api_key = APIKeyManager.get_api_key_from_context()
            # Use api_key for USPTO API call
    """

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # Extract and validate API key
        api_key = APIKeyManager.get_api_key_from_context()

        if not APIKeyManager.validate_api_key_format(api_key):
            raise AuthenticationError(
                code="INVALID_API_KEY_FORMAT",
                message="API key format is invalid",
                details={"hint": "API key must be at least 20 characters"},
            )

        # API key is valid, proceed with tool execution
        return await func(*args, **kwargs)

    return wrapper


def rate_limit(
    endpoint_category: str,
) -> Callable[
    [Callable[..., Awaitable[Any]]],
    Callable[..., Awaitable[Any]],
]:
    """
    Decorator to enforce rate limits on MCP tools.

    Args:
        endpoint_category: Category for rate limiting (e.g., "search", "retrieval")

    The decorator stores rate limit info in context state for later retrieval.
    Use get_rate_limit_info() to access the current rate limit state.

    Usage:
        @mcp.tool()
        @require_api_key
        @rate_limit("search")
        async def uspto_applications_search(request: SearchRequest):
            # Rate limit info is available via get_rate_limit_info()
            ...
    """

    def decorator(
        func: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Check rate limit
            result = rate_limiter.check_rate_limit(endpoint_category)

            if not result.allowed:
                raise RateLimitError(
                    limit=result.limit,
                    retry_after=result.retry_after,
                    reset_at=result.reset_at,
                )

            # Store rate limit info for retrieval
            rate_limit_info = RateLimitInfo(
                limit=result.limit,
                remaining=result.remaining,
                reset_at=result.reset_at,
                endpoint_category=endpoint_category,
            )

            # Store in ContextVar (task-local, safe for concurrent async)
            _current_rate_limit_info.set(rate_limit_info)

            # Try to store in FastMCP context state if available
            try:
                from fastmcp.server.dependencies import get_context

                ctx = get_context()
                ctx.set_state(RATE_LIMIT_STATE_KEY, rate_limit_info)
            except (RuntimeError, ImportError):
                # No context available (e.g., in tests or non-HTTP transport)
                pass

            # Rate limit passed, execute the function
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def get_rate_limit_info() -> RateLimitInfo | None:
    """
    Retrieve the current rate limit information.

    Returns the rate limit info from the most recent rate-limited call.
    This can be used to include rate limit headers in responses.

    Returns:
        RateLimitInfo if available, None otherwise

    Usage:
        @mcp.tool()
        @rate_limit("search")
        async def search_tool(request: SearchRequest) -> SearchResponse:
            result = do_search(request)

            # Get rate limit info to include in response
            rate_info = get_rate_limit_info()
            if rate_info:
                # Include in response metadata
                ...

            return result
    """
    # Try to get from FastMCP context first
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        info = ctx.get_state(RATE_LIMIT_STATE_KEY)
        if info is not None:
            return info
    except (RuntimeError, ImportError):
        pass

    # Fall back to ContextVar (task-local storage)
    return _current_rate_limit_info.get()


def clear_rate_limit_info() -> None:
    """
    Clear the stored rate limit info from all storage locations.

    Clears both the ContextVar and FastMCP context state to ensure
    consistent behavior. Primarily used for testing.
    """
    # Clear ContextVar (task-local storage)
    _current_rate_limit_info.set(None)

    # Also clear FastMCP context state if available
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        ctx.set_state(RATE_LIMIT_STATE_KEY, None)
    except (RuntimeError, ImportError):
        # No context available
        pass


__all__ = [
    "clear_rate_limit_info",
    "get_rate_limit_info",
    "rate_limit",
    "require_api_key",
]
