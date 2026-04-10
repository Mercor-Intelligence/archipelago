"""Convenience function for setting up authentication on FastMCP servers."""

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

from .decorators import public_tool
from .middleware.auth_guard import AuthGuard
from .services.auth_service import AuthService
from .tools.auth_tools import create_login_tool

if TYPE_CHECKING:
    from fastmcp import FastMCP


def setup_auth(
    mcp_instance: "FastMCP",
    users_file: Path | str | None = None,
    token_prefix: str | None = None,
) -> tuple[AuthService, AuthGuard]:
    """
    Configure authentication and authorization for an MCP server.

    This is a convenience function that:
    1. Creates an AuthService instance
    2. Registers a public login_tool
    3. Adds AuthGuard middleware with auto-discovery

    Args:
        mcp_instance: FastMCP instance to configure
        users_file: Path to users.json file. If None, attempts to infer from
                   the calling module's location (looks for users.json in the
                   parent directory of the caller's file).
        token_prefix: Optional prefix to embed in issued tokens (e.g.
                      "greenhouse") so persona-specific format matching is
                      easier to assert in tests.

    Returns:
        tuple: (auth_service, auth_guard) for further configuration if needed

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
    """
    if users_file is None:
        # Infer users.json location from calling module
        # Walk up the call stack to find the actual caller
        frame = inspect.currentframe()
        try:
            # Skip this function (f_back) and any wrapper functions
            # Look for the first frame that's not in this package
            caller_frame = frame.f_back if frame and frame.f_back else None
            if caller_frame:
                caller_file = Path(caller_frame.f_code.co_filename)
                # Try multiple locations:
                # 1. Same directory as caller (if called from main.py)
                # 2. Parent directory (if called from middleware/auth.py)
                # 3. Parent's parent (if called from deeper)
                for parent_level in [0, 1, 2]:
                    candidate = caller_file.parent
                    for _ in range(parent_level):
                        candidate = candidate.parent
                    candidate_file = candidate / "users.json"
                    if candidate_file.exists():
                        users_file = candidate_file
                        break
                else:
                    # Fallback: assume server root is parent of caller's directory
                    # (works for middleware/auth.py pattern)
                    users_file = caller_file.parent.parent / "users.json"
            else:
                # Fallback: current directory
                users_file = Path("users.json")
        finally:
            del frame
    elif isinstance(users_file, str):
        # If string, resolve relative to caller's directory
        frame = inspect.currentframe()
        try:
            caller_frame = frame.f_back if frame and frame.f_back else None
            if caller_frame:
                caller_dir = Path(caller_frame.f_code.co_filename).parent
                users_file = (caller_dir / users_file).resolve()
            else:
                users_file = Path(users_file).resolve()
        finally:
            del frame
    else:
        # Already a Path object
        users_file = Path(users_file)

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
