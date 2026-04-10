"""Greenhouse MCP Server Authentication Module.

This module provides permission constants for the Greenhouse MCP server.
Authentication and authorization are handled by mcp_auth package.

For testing with mock users, use mcp_auth testing utilities:
    from mcp_auth import mock_auth_user, mock_recruiter, mock_hiring_manager

For auth errors:
    from mcp_auth import AuthenticationError, AuthorizationError
"""

# Re-export errors from mcp_auth for backwards compatibility
from auth.permissions import Permission
from mcp_auth import AuthenticationError, AuthorizationError

__all__ = [
    "Permission",
    "AuthenticationError",
    "AuthorizationError",
]
