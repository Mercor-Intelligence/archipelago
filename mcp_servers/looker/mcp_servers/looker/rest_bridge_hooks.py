"""REST bridge hooks for Looker MCP server.

Registers UI routes (RLS world data import endpoints) with the REST bridge.
"""

from fastapi import FastAPI

from .ui_routes import get_router


def register_endpoints(app: FastAPI, module_path: str, engine=None):
    """Register custom REST endpoints for Looker.

    Args:
        app: The FastAPI application instance
        module_path: The MCP server module path (e.g., 'mcp_servers.looker.main')
        engine: Optional SQLAlchemy database engine (None if no database)
    """
    # Include the UI routes router (/api/rls/* endpoints)
    router = get_router()
    app.include_router(router)
