"""MCP Server: __PASCAL_NAME__

This server uses the repository pattern for data access:
- Offline mode (default): Uses synthetic data from JSON files
- Online mode: Makes live API calls
- Authentication: Requires login to access protected tools

Set __UPPER_NAME___MODE=online to use live API.

Tool granularity:
- Set TOOLS env var to comma-separated list to enable specific tools
- Example: TOOLS="get___SNAKE_NAME__,list___SNAKE_NAME__"
- If TOOLS is empty or not set, all tools are enabled
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)

# Middleware imports
from mcp_middleware import LoggingMiddleware
from mcp_middleware.db_tools import create_database_tools

# Authentication imports (requires: pip install -e ../../packages/mcp_auth)
from mcp_auth import create_login_tool, public_tool, require_scopes
from middleware.auth import setup_auth
from tools.__SNAKE_NAME__ import __SNAKE_NAME__, get___SNAKE_NAME__, list___SNAKE_NAME__
from db.session import init_db


@asynccontextmanager
async def lifespan(app):
    """Initialize database on server startup."""
    await init_db()
    yield


mcp = FastMCP("__PASCAL_NAME__", lifespan=lifespan)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Database management tools (import_csv, export_csv, list_tables, clear_database)
create_database_tools(mcp, "db.session")

# Setup authentication (must be done before creating login tool)
# Returns None if authentication is disabled (e.g., requires_auth: false in build spec)
auth_service = setup_auth(mcp, users_file="users.json")

# Authentication Tools (only registered if auth is enabled)
if auth_service is not None:
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
