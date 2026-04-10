"""Authentication setup using mcp-auth package.

This file provides helper functions for setting up authentication.
See: packages/mcp_auth/README.md for full documentation.
"""

from pathlib import Path

from mcp_auth import AuthGuard, AuthService


def setup_auth(mcp_instance, users_file: str = "users.json"):
    """
    Setup authentication for the MCP server.

    Args:
        mcp_instance: The FastMCP server instance
        users_file: Path to users.json file (default: "users.json")

    Usage in main.py or ui.py:
        from middleware.auth import setup_auth

        # After registering all tools
        setup_auth(mcp, users_file="users.json")
    """
    users_path = Path(__file__).parent.parent / users_file
    auth_service = AuthService(users_path)

    # Add AuthGuard middleware with auto-discovery
    auth_guard = AuthGuard(
        auth_service,
        mcp_instance=mcp_instance,  # Auto-discovers permissions from decorators
        public_tools=["login_tool"],  # Tools that don't require auth
        default_deny=True,  # Deny tools without explicit permissions
    )

    mcp_instance.add_middleware(auth_guard)

    return auth_service
