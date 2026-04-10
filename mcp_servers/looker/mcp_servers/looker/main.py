"""Looker MCP Server - Conditional Tool Registration.

This server provides both meta-tools (for LLMs) and individual tools (for UI).

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 8 meta-tools for LLM agents
- GUI_ENABLED=true: Individual tools for UI display

Meta-Tools (8 total):
| Tool              | Actions                                                           |
|-------------------|-------------------------------------------------------------------|
| looker_lookml     | list_models, get_explore, list_views, generate, deploy, etc.      |
| looker_content    | list_folders, search, list_explores, list_fields                  |
| looker_queries    | create, run_inline, run_by_id, run_png, export, sql               |
| looker_looks      | list, get, create, run, render_pdf                                |
| looker_dashboards | list, get, create, add_tile, export_pdf, export_png, download     |
| looker_admin      | health                                                            |
| looker_schema     | (introspection - returns JSON schema for any tool)                |
| upload_csv        | Upload CSV data and make it queryable as LookML                   |

Note: V2 tools (tools/v2/) are NOT registered directly. They are used internally
by meta-tools but not exposed as standalone MCP tools.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from config import settings
from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from mcp_middleware import ServerConfig, run_server
from middleware.data_layer_init import DataLayerInitMiddleware
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware


@asynccontextmanager
async def lifespan(mcp: FastMCP) -> AsyncIterator[None]:
    """Server lifespan — restore state quickly, then init data layer in background.

    Order matters for fast startup:
    1. Restore persisted state (fast — reads JSON files from disk).
       This populates dashboards, looks, queries, and tiles so listing
       tools return data immediately.
    2. Yield — server starts accepting connections NOW.
    3. Data layer init (slow — copies DuckDB, parses LookML, queries schema)
       runs in a background task. The DataLayerInitMiddleware blocks only
       tools that actually need the data layer (explore, query, etc.).
    """
    import asyncio

    from http_client import close_http_client
    from loguru import logger

    # Step 1: Restore persisted dashboards, looks, queries, and tiles from disk.
    # This is fast (JSON reads) and makes listing tools work immediately.
    try:
        from state_persistence import restore_persisted_state

        await asyncio.to_thread(restore_persisted_state)
    except Exception as e:
        logger.warning(f"Failed to restore persisted state: {e}")

    # Step 2: Start data layer init in background so server accepts connections
    # immediately. The DataLayerInitMiddleware will wait for this to complete
    # before allowing tools that need the data layer (explore/query tools).
    async def _background_data_layer_init():
        try:
            from data_layer import initialize_data_layer

            await asyncio.to_thread(initialize_data_layer)
            logger.info("Data layer initialized (background)")
        except Exception as e:
            logger.warning(f"Background data layer init failed (middleware will retry): {e}")

    asyncio.create_task(_background_data_layer_init())

    yield
    # Cleanup on shutdown
    await close_http_client()


mcp = FastMCP("Looker", lifespan=lifespan)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(DataLayerInitMiddleware())  # Init data layer on first tool call
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())


def register_tools():
    """Register tools on the MCP instance based on GUI_ENABLED.

    GUI_ENABLED=true: Individual tools for UI display
    GUI_ENABLED=false (default): 8 meta-tools for LLM agents
    """
    # Mutually exclusive: GUI_ENABLED gets UI tools, otherwise meta-tools
    if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
        # ===== Register Individual Tools (for UI) =====
        from tools import (
            _search_content,
            add_tile_to_dashboard,
            create_dashboard,
            create_look,
            create_query,
            deploy_lookml,
            export_query,
            generate_lookml,
            get_dashboard,
            get_explore,
            get_generated_lookml,
            get_look,
            health_check,
            list_available_views,
            list_dashboards,
            list_explores,
            list_fields,
            list_folders,
            list_lookml_models,
            list_looks,
            run_dashboard,
            run_look,
            run_query_by_id,
            run_query_inline,
            run_sql_query,
        )
        from tools.content_rendering import run_dashboard_pdf, run_look_pdf
        from tools.csv_upload import upload_csv
        from tools.lookml_discovery import list_views
        from tools.query_execution import run_query_png
        from tools.reload_data import reload_data

        # LookML Discovery
        mcp.tool(list_lookml_models)
        mcp.tool(get_explore)
        mcp.tool(list_views)

        # LookML Management
        mcp.tool(generate_lookml)
        mcp.tool(get_generated_lookml)
        mcp.tool(list_available_views)
        mcp.tool(deploy_lookml)

        # Content Discovery
        mcp.tool(list_folders)
        mcp.tool(_search_content)
        mcp.tool(list_explores)
        mcp.tool(list_fields)

        # Look Management
        mcp.tool(list_looks)
        mcp.tool(get_look)
        mcp.tool(create_look)
        mcp.tool(run_look)
        mcp.tool(run_look_pdf)

        # Dashboard Management
        mcp.tool(list_dashboards)
        mcp.tool(get_dashboard)
        mcp.tool(create_dashboard)
        mcp.tool(add_tile_to_dashboard)
        mcp.tool(run_dashboard)
        mcp.tool(run_dashboard_pdf)

        # Query Execution
        mcp.tool(create_query)
        mcp.tool(run_query_inline)
        mcp.tool(run_query_by_id)
        mcp.tool(run_query_png)
        mcp.tool(export_query)
        mcp.tool(run_sql_query)

        # Admin & Health
        mcp.tool(health_check)

        # Data Import
        mcp.tool(upload_csv)
        mcp.tool(reload_data)

    else:
        # ===== Register Meta-Tools (for LLMs) =====
        from tools._meta_tools import (
            looker_admin,
            looker_content,
            looker_dashboards,
            looker_lookml,
            looker_looks,
            looker_queries,
            looker_schema,
        )
        from tools.csv_upload import upload_csv

        mcp.tool(looker_lookml)
        mcp.tool(looker_content)
        mcp.tool(looker_queries)
        mcp.tool(looker_looks)
        mcp.tool(looker_dashboards)
        mcp.tool(looker_admin)
        mcp.tool(looker_schema)
        mcp.tool(upload_csv)


def main():
    """Register tools and start the Looker MCP server."""
    register_tools()

    # Derive mode from settings
    if settings.is_hybrid_mode():
        mode = "hybrid"
    elif settings.is_offline_mode():
        mode = "offline"
    else:
        mode = "online"

    config = ServerConfig(
        name="looker",
        version="1.0.0",
        description=(
            "MCP server for Looker BI: explore the semantic layer (models, explores, "
            "fields), execute queries, create/manage Looks and Dashboards, render "
            "visualizations as PNG/PDF, and upload CSV data for analysis"
        ),
        mode=mode,
        features={"persistence": "duckdb"},
        paginate_tools=["*list*", "run_sql_query", "run_query_inline"],
    )
    run_server(mcp, config=config)


if __name__ == "__main__":
    main()
