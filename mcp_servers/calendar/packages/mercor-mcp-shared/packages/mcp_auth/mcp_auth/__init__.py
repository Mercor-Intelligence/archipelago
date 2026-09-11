"""
MCP Auth - Shared authentication for MCP servers.

This package provides reusable authentication middleware, services,
and tools for FastMCP servers.
"""

from .context import (
    current_user,
    get_current_user,
    get_http_headers,
    http_headers_var,
    is_auth_enabled,
    set_http_headers,
    user_has_role,
    user_has_scope,
)
from .decorators import (
    public_tool,
    require_any_scopes,
    require_roles,
    require_scopes,
    session_login,
)
from .errors import AuthenticationError, AuthorizationError
from .middleware.auth_guard import AuthGuard
from .oauth_pkce import OAuthPKCEManager, TokenResponse
from .services.auth_service import AuthService
from .setup import is_auth_configured, setup_auth
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
    "get_http_headers",
    "http_headers_var",
    "is_auth_configured",
    "is_auth_enabled",
    "mock_auth_user",
    "OAuthPKCEManager",
    "public_tool",
    "require_any_scopes",
    "require_roles",
    "require_scopes",
    "session_login",
    "set_http_headers",
    "setup_auth",
    "TokenResponse",
    "user_has_role",
    "user_has_scope",
    "with_scope_enforcement",
]
