"""User context management for MCP auth.

Provides a ContextVar to store the current authenticated user, making user
information available to tools without requiring manual middleware setup.
"""

from contextvars import ContextVar

# Context variable to store current authenticated user
# Set automatically by AuthGuard after successful authentication
current_user: ContextVar[dict] = ContextVar("current_user", default={})


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


__all__ = ["current_user", "get_current_user"]
