"""Convenience function for setting up authentication on FastMCP servers."""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .decorators import public_tool
from .middleware.auth_guard import AuthGuard
from .services.auth_service import AuthService
from .tools.auth_tools import create_login_tool

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def is_auth_configured() -> bool:
    """Check if authentication is enabled via environment variables.

    This checks the configuration/environment at startup, not runtime state.
    Use this for server_info responses and setup decisions.

    For runtime checks (whether a user is currently authenticated), use
    is_auth_enabled() from mcp_auth.context instead.

    Priority:
    1. DISABLE_AUTH=true/1/yes -> auth disabled
    2. ENABLE_AUTH=true/1/yes -> auth enabled
    3. MCP_UI_GEN=true -> auth enabled (for UI generation to include login_tool)
    4. Default: disabled (auth is opt-in)
    """
    disable_auth = os.getenv("DISABLE_AUTH", "").lower() in ("true", "1", "yes")
    if disable_auth:
        return False

    enable_auth = os.getenv("ENABLE_AUTH", "").lower() in ("true", "1", "yes")
    if enable_auth:
        return True

    # Enable auth during UI generation so login_tool is included in the schema
    ui_gen = os.getenv("MCP_UI_GEN", "").lower() in ("true", "1", "yes")
    return ui_gen


def setup_auth(
    mcp_instance: "FastMCP",
    users_file: Path | str | None = None,
    token_prefix: str | None = None,
    auth_service: AuthService | None = None,
) -> tuple[AuthService, AuthGuard] | None:
    """
    Configure authentication and authorization for an MCP server.

    Authentication is controlled via environment variables:
    - DISABLE_AUTH=true/1/yes -> auth disabled (takes priority)
    - ENABLE_AUTH=true/1/yes -> auth enabled
    - Default: disabled (auth is opt-in)

    This is a convenience function that:
    1. Checks if auth is enabled via environment variables
    2. Creates an AuthService instance
    3. Registers a public login_tool
    4. Adds AuthGuard middleware with auto-discovery

    Args:
        mcp_instance: FastMCP instance to configure
        users_file: Path to users.json file. If None, defaults to users.json
                   in the current working directory. When called via run_server(),
                   this is automatically set to the server's directory.
        token_prefix: Optional prefix to embed in issued tokens (e.g.
                      "greenhouse") so persona-specific format matching is
                      easier to assert in tests.

    Returns:
        tuple: (auth_service, auth_guard) for further configuration if needed,
               or None if auth is disabled.

    Usage:
        from mcp_auth import setup_auth

        # Register tools first
        @mcp.tool()
        @require_scopes("read:data")
        async def my_tool():
            ...

        # Then setup auth (after tool registration for decorator discovery)
        # Option 1: Auto-detect users.json from caller location
        setup_auth(mcp)

        # Option 2: Explicit path
        setup_auth(mcp, users_file=Path("path/to/users.json"))
        setup_auth(mcp, users_file="users.json")  # Relative to caller

    Environment variables:
        ENABLE_AUTH: Set to "true", "1", or "yes" to enable authentication
        DISABLE_AUTH: Set to "true", "1", or "yes" to disable authentication
                      (takes priority over ENABLE_AUTH)
    """
    # Check if auth is enabled via environment variables
    if not is_auth_configured():
        logger.info("Authentication disabled (ENABLE_AUTH not set or DISABLE_AUTH=true)")
        return None

    logger.info("Authentication enabled (ENABLE_AUTH=true)")

    # Use provided auth_service or create a new one
    if auth_service is None:
        # Resolve users_file path
        # When called via run_server(), users_file is already an absolute path
        # When called directly, fall back to cwd
        if users_file is None:
            users_file = Path.cwd() / "users.json"
        elif isinstance(users_file, str):
            users_file = Path(users_file)
        # else: already a Path object

        auth_service = AuthService(users_file, token_prefix=token_prefix)

    # Register public login tool (named "login_tool" per codebase convention)
    mcp_instance.tool(name="login_tool")(public_tool(create_login_tool(auth_service)))

    # Add auth guard middleware (discovers @public_tool and @require_scopes decorators)
    auth_guard = AuthGuard(
        auth_service=auth_service,
        mcp_instance=mcp_instance,
        default_deny=True,
    )
    mcp_instance.add_middleware(auth_guard)

    return auth_service, auth_guard
