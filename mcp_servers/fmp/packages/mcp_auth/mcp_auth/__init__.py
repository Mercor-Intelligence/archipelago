"""
MCP Auth - Shared authentication for MCP servers.

This package provides reusable authentication middleware, services,
and tools for FastMCP servers.
"""

from .decorators import public_tool, require_roles, require_scopes
from .middleware.auth_guard import AuthGuard
from .services.auth_service import AuthService
from .tools.auth_tools import create_login_tool
from .version import __version__

__all__ = [
    "__version__",
    "AuthGuard",
    "AuthService",
    "create_login_tool",
    "require_scopes",
    "require_roles",
    "public_tool",
]
