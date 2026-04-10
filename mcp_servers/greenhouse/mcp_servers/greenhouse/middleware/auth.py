"""Authentication middleware for Greenhouse MCP server.

Uses the shared mcp_auth package from packages/mcp_auth/ for:
- AuthService: User validation and token management
- AuthGuard: Middleware that validates tokens and enforces RBAC
- Decorators: @require_scopes, @require_roles, @public_tool
- setup_auth: Convenience function for configuring authentication

Scopes follow the pattern: resource:action
See users.json for persona definitions and scope assignments.
"""

from pathlib import Path

from mcp_auth import setup_auth as _setup_auth

# Path to users.json relative to this file
USERS_FILE = Path(__file__).parent.parent / "users.json"


def setup_auth(mcp_instance):
    """
    Configure authentication and authorization for the Greenhouse MCP server.

    This is a thin wrapper around mcp_auth.setup_auth that provides
    server-specific users.json path.

    Args:
        mcp_instance: FastMCP instance to configure

    Returns:
        tuple: (auth_service, auth_guard) for further configuration if needed
    """
    return _setup_auth(
        mcp_instance,
        users_file=USERS_FILE,
        token_prefix="greenhouse",
    )


__all__ = ["setup_auth", "USERS_FILE"]
