"""
MCP Auth - Shared authentication for MCP servers.

This package provides reusable authentication middleware, services,
and tools for FastMCP servers.
"""

from .context import current_user, get_current_user
from .decorators import (
    public_tool,
    require_any_scopes,
    require_roles,
    require_scopes,
    session_login,
)
from .errors import AuthenticationError, AuthorizationError
from .middleware.auth_guard import AuthGuard
from .services.auth_service import AuthService
from .setup import setup_auth
from .testing import (
    mock_auth_user,
    with_scope_enforcement,
)
from .tools.auth_tools import create_login_tool
from .version import __version__

__all__ = [
    "__version__",
    "AuthenticationError",
    "AuthGuard",
    "AuthorizationError",
    "AuthService",
    "create_login_tool",
    "current_user",
    "get_current_user",
    "mock_auth_user",
    "public_tool",
    "require_any_scopes",
    "require_roles",
    "require_scopes",
    "session_login",
    "setup_auth",
    "with_scope_enforcement",
]
