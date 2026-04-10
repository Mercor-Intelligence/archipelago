"""__PASCAL_NAME__ MCP Server - LLM Entrypoint.

Exposes tools for LLM consumption via Claude Desktop or other AI clients.
For GUI/REST API, use ui.py instead.

This server uses the repository pattern for data access:
- Offline mode (default): Uses synthetic data from JSON files
- Online mode: Makes live API calls
- Authentication: Requires login to access protected tools

Set __UPPER_NAME___MODE=online to use live API.
"""

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)

# Middleware imports
from mcp_middleware import LoggingMiddleware

# Authentication imports (requires: pip install -e ../../packages/mcp_auth)
from mcp_auth import create_login_tool, public_tool, require_scopes
from middleware.auth import setup_auth
from tools.__SNAKE_NAME__ import __SNAKE_NAME__, get___SNAKE_NAME__, list___SNAKE_NAME__

mcp = FastMCP("__PASCAL_NAME__")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Setup authentication (must be done before creating login tool)
auth_service = setup_auth(mcp, users_file="users.json")

# Authentication Tools
login_func = create_login_tool(auth_service)


@mcp.tool(name="login_tool")
async def login_tool_wrapper(username: str, password: str) -> dict:
    """Login with username and password to get an access token."""
    return await login_func(username, password)


# Example: Public tool (no authentication required)
@mcp.tool()
@public_tool
async def get_server_info() -> dict:
    """Get public server information. No authentication required."""
    return {
        "name": "__PASCAL_NAME__",
        "status": "running",
        "features": {
            "authentication": True,
            "repository_pattern": True,
        },
    }


# Example: Protected tool requiring 'read' scope
@mcp.tool()
@require_scopes("read")
async def read_data() -> dict:
    """
    Read data from the system.

    Required scope: read
    """
    return {"data": ["item1", "item2", "item3"], "count": 3}


# Tool granularity: set TOOLS env var to comma-separated list to enable specific tools
enabled_tools = os.getenv("TOOLS", "").split(",")
enabled_tools = [t.strip() for t in enabled_tools if t.strip()]

# Register tools conditionally based on TOOLS env var
if not enabled_tools or "__SNAKE_NAME__" in enabled_tools:
    mcp.tool(__SNAKE_NAME__)

if not enabled_tools or "get___SNAKE_NAME__" in enabled_tools:
    mcp.tool(get___SNAKE_NAME__)

if not enabled_tools or "list___SNAKE_NAME__" in enabled_tools:
    mcp.tool(list___SNAKE_NAME__)

# To add more tools with granularity:
# from tools.other_module import other_tool
# if not enabled_tools or "other_tool" in enabled_tools:
#     mcp.tool(other_tool)

if __name__ == "__main__":
    mcp.run()
