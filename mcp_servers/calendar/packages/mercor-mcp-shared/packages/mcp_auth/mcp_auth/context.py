"""User context management for MCP auth.

Provides ContextVars to store:
- Current authenticated user (set by AuthGuard)
- HTTP headers (set by RestBridgeMiddleware for auth token extraction)
"""

from contextvars import ContextVar

# Context variable to store current authenticated user
# Set automatically by AuthGuard after successful authentication
current_user: ContextVar[dict] = ContextVar("current_user", default={})

# Context variable to store HTTP headers for authentication
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


def get_current_user() -> dict:
    """Get the current authenticated user from context.

    Returns:
        User dict with userId, username, roles, scopes, and any custom fields
        (like employeeId). Returns empty dict if no user is authenticated.

    Example:
        >>> from mcp_auth import get_current_user
        >>> user = get_current_user()
        >>> user_id = user.get("userId")
        >>> roles = user.get("roles", [])
    """
    return current_user.get()


def is_auth_enabled() -> bool:
    """Check if authentication is enabled.

    Returns True if a user context has been set (auth middleware is active),
    False if no user context exists (auth is disabled or not configured).

    Use this in tools that have internal permission checks to bypass those
    checks when auth is disabled.

    Example:
        >>> from mcp_auth import is_auth_enabled
        >>> if not is_auth_enabled():
        ...     # Auth disabled, allow all access
        ...     return True, None
        >>> # Otherwise, perform normal permission checks
    """
    user = current_user.get()
    return bool(user)


def user_has_role(*required_roles: str) -> bool:
    """Check if user has at least one of the required roles.

    Returns True if:
    - Auth is disabled (no user context), OR
    - User has at least one of the required roles

    This provides a simple way to check roles without worrying about
    whether auth is enabled or disabled.

    Args:
        *required_roles: One or more role names to check for

    Example:
        >>> from mcp_auth import user_has_role
        >>> if not user_has_role("hr_admin", "manager"):
        ...     return {"error": {"code": 403, "message": "Access denied"}}
    """
    user = current_user.get()

    # Auth disabled - allow all access
    if not user:
        return True

    user_roles = set(user.get("roles") or [])
    return bool(user_roles & set(required_roles))


def user_has_scope(*required_scopes: str) -> bool:
    """Check if user has all of the required scopes.

    Returns True if:
    - Auth is disabled (no user context), OR
    - User has ALL of the required scopes

    Args:
        *required_scopes: One or more scope names to check for (all required)

    Example:
        >>> from mcp_auth import user_has_scope
        >>> if not user_has_scope("read:employees"):
        ...     return {"error": {"code": 403, "message": "Access denied"}}
    """
    user = current_user.get()

    # Auth disabled - allow all access
    if not user:
        return True

    user_scopes = set(user.get("scopes") or [])
    return set(required_scopes) <= user_scopes


__all__ = [
    "current_user",
    "get_current_user",
    "get_http_headers",
    "http_headers_var",
    "is_auth_enabled",
    "set_http_headers",
    "user_has_role",
    "user_has_scope",
]
