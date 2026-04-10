from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from loguru import logger

# Import from shared mcp-auth package
from mcp_auth import (
    AuthGuard,
    AuthService,
    create_login_tool,
    public_tool,
    require_roles,
    require_scopes,
)
from starlette.responses import JSONResponse

# Import local tools
from tools.item_tools import get_item, list_items

# Configuration
SERVER_VERSION = "1.0.0"
MCP_AUTH_VERSION = "1.0.0"

# Initialize AuthService with local users
users_file = Path(__file__).parent / "users.json"
auth_service = AuthService(users_file)

# Create MCP server
mcp = FastMCP("AuthServer")

# Public Tools (No Authentication Required)


@mcp.tool()
@public_tool
async def get_server_info() -> dict:
    """
    Get public server information.

    Returns server version, status, and capabilities.
    No authentication required.
    """
    return {
        "version": SERVER_VERSION,
        "status": "running",
        "mcp_auth_version": MCP_AUTH_VERSION,
        "capabilities": {
            "authentication": True,
            "rbac": True,
            "scopes": ["read", "write", "delete"],
        },
    }


# Authentication Tools

# Register auth tools from shared package
login_func = create_login_tool(auth_service)


@mcp.tool(name="login_tool")
async def login_tool_wrapper(username: str, password: str) -> dict:
    """Login with username and password to get an access token."""
    return await login_func(username, password)


# Protected Tools - Scope-Based Authorization


@mcp.tool()
@require_scopes("read")
async def read_data() -> dict:
    """
    Read data from the system.

    Required scope: read

    Returns:
        List of data items
    """
    logger.info("Reading data")
    return {"data": ["item1", "item2", "item3"], "count": 3}


@mcp.tool()
@require_scopes("write")
async def write_data(data: str) -> dict:
    """
    Write data to the system.

    Required scope: write

    Args:
        data: Data to write

    Returns:
        Confirmation of write operation
    """
    logger.info(f"Writing data: {data}")
    return {"status": "written", "data": data}


@mcp.tool()
@require_scopes("read", "write")
async def update_data(item_id: int, new_data: str) -> dict:
    """
    Update existing data.

    Required scopes: read, write

    Args:
        item_id: ID of item to update
        new_data: New data value

    Returns:
        Updated item information
    """
    logger.info(f"Updating item {item_id} with: {new_data}")
    return {"status": "updated", "item_id": item_id, "data": new_data}


# Protected Tools - Role and Scope Based Authorization


@mcp.tool()
@require_roles("admin")
@require_scopes("read", "write")
async def list_items_tool() -> dict:
    """
    List all items in the system.

    Required role: admin
    Required scopes: read, write

    Returns:
        List of all items
    """
    logger.info("Admin listing all items")
    return await list_items()


@mcp.tool()
@require_roles("user", "admin")
@require_scopes("read")
async def get_item_tool(item_id: int) -> dict:
    """
    Get details of a specific item by ID.

    Required roles: user OR admin
    Required scope: read

    Args:
        item_id: ID of the item to retrieve

    Returns:
        Item details or error if not found
    """
    logger.info(f"Getting item: {item_id}")
    return await get_item(item_id)


@mcp.tool()
@require_roles("admin")
@require_scopes("delete")
async def delete_item(item_id: int) -> dict:
    """
    Delete an item by ID.

    Required role: admin
    Required scope: delete

    Args:
        item_id: ID of item to delete

    Returns:
        Deletion confirmation
    """
    logger.warning(f"Admin deleting item: {item_id}")
    return {"status": "deleted", "item_id": item_id}


# Middleware Setup

# Add error handling middleware first
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))

# Add AuthGuard with auto-discovery AFTER registering all tools
mcp.add_middleware(
    AuthGuard(
        auth_service,
        mcp_instance=mcp,  # Auto-discovers permissions from decorators
        public_tools=["login_tool"],  # Additional public tools
        default_deny=True,  # Deny tools without explicit permissions
    )
)


# HTTP Endpoints


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint."""
    return JSONResponse(
        {
            "status": "healthy",
            "service": "mcp-auth-server",
            "version": SERVER_VERSION,
            "mcp_auth_version": MCP_AUTH_VERSION,
        }
    )


@mcp.custom_route("/info", methods=["GET"])
async def server_info(request):
    """Server information endpoint."""
    return JSONResponse(
        {
            "name": "MCP Auth Server",
            "version": SERVER_VERSION,
            "description": "Example MCP server with authentication and authorization",
            "features": {
                "authentication": "Bearer token",
                "authorization": "RBAC with scopes",
                "token_expiry": "1 hour",
            },
            "endpoints": {
                "health": "/health",
                "info": "/info",
                "mcp": "/mcp",
            },
        }
    )


# ---------------------------------------------------------------------------
# Gemini compatibility: inline $defs/$ref in tool schemas
# ---------------------------------------------------------------------------
def _inline_schema_refs(obj, defs=None, seen=None):
    """Recursively inline $ref → $defs so Gemini can parse tool schemas."""
    if seen is None:
        seen = set()

    if isinstance(obj, dict):
        local_defs = obj.get("$defs", defs)
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and local_defs:
            ref_key = ref.split("/")[-1]
            if ref_key in local_defs:
                if ref_key in seen:
                    # Break infinite recursion for recursive models
                    return {"type": "object", "description": f"Recursive reference to {ref_key}"}

                import copy

                return _inline_schema_refs(
                    copy.deepcopy(local_defs[ref_key]), local_defs, seen | {ref_key}
                )
        return {k: _inline_schema_refs(v, local_defs, seen) for k, v in obj.items() if k != "$defs"}

    if isinstance(obj, list):
        return [_inline_schema_refs(i, defs, seen) for i in obj]

    return obj


for _tool in mcp._tool_manager._tools.values():  # noqa: SLF001
    if isinstance(_tool.parameters, dict):
        _tool.parameters = _inline_schema_refs(_tool.parameters)
    if isinstance(getattr(_tool, "output_schema", None), dict):
        _tool.output_schema = _inline_schema_refs(_tool.output_schema)


if __name__ == "__main__":
    logger.info("Starting MCP Auth Server")
    logger.info(f"Server Version: {SERVER_VERSION}")
    logger.info(f"MCP Auth Version: {MCP_AUTH_VERSION}")

    mcp.run(transport="http", stateless_http=True)
