"""Tableau MCP Server - Main entry point.

Manage Tableau Server/Cloud resources including sites, users, groups, projects,
workbooks, datasources, views, and permissions. Supports workbook publishing,
view data/image export, and programmatic visualization creation via drag-and-drop
shelf configuration.

Exposes 9 meta-tools for LLM context optimization:
- tableau_admin: Sites and permissions management (list_sites, grant_permission, list_permissions, revoke_permission)
- tableau_users: User CRUD operations (create, list, get, update, delete)
- tableau_projects: Project CRUD operations (create, list, get, update, delete)
- tableau_workbooks: Workbook CRUD, publish, and connections (create, list, get, update, delete, publish, connect, list_connections, disconnect)
- tableau_views: View queries and image export (list, get, metadata, query_to_file, image)
- tableau_datasources: Datasource CRUD operations (create, list, get, update, delete)
- tableau_groups: Group and membership management (create, list, add_user, remove_user)
- tableau_visualization: CSV upload, shelf config, chart generation (upload_csv, get_sheets, list_fields, configure_shelf, create_visualization, create_sheet)
- tableau_schema: Tool introspection (get input/output schemas for any tool)

All resource identifiers use UUID v4 format (36-character strings with hyphens).
All timestamps are ISO 8601 format with UTC timezone.
"""

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from db.session import init_db
from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from loguru import logger
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

# Reconfigure loguru: only emit WARNING+ to stderr.  The platform reads
# stderr via process.stderr.readline() and too much output fills the
# pipe buffer (~64KB), blocking the server.  stdout is reserved for
# JSON-RPC (stdio transport) so logs must never go there.
logger.remove()
logger.add(sys.stderr, level="WARNING")

# Also suppress standard-library loggers (used by visualization_tools,
# db/session, publish_workbook_tools, and third-party libs like sqlalchemy,
# matplotlib, uvicorn).  These bypass loguru and would otherwise write
# DEBUG/INFO to stdout (corrupting JSON-RPC) or fill the stderr pipe buffer.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
from tools._meta_tools import (  # noqa: E402
    tableau_admin,
    tableau_datasources,
    tableau_groups,
    tableau_projects,
    tableau_schema,
    tableau_users,
    tableau_views,
    tableau_visualization,
    tableau_workbooks,
)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize database on startup."""
    await init_db()
    yield {}


mcp = FastMCP("Tableau", lifespan=lifespan)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=False))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

# Determine tool registration mode
GUI_ENABLED = os.getenv("GUI_ENABLED") == "true"

# ============================================================================
# Conditional Tool Registration
# ============================================================================
if GUI_ENABLED:
    # ========================================================================
    # UI MODE: Individual granular tools for human users
    # ========================================================================
    from tools.connection_tools import (
        tableau_create_workbook_connection,
        tableau_delete_workbook_connection,
        tableau_list_workbook_connections,
    )
    from tools.datasource_tools import (
        tableau_create_datasource,
        tableau_delete_datasource,
        tableau_get_datasource,
        tableau_list_datasources,
        tableau_update_datasource,
    )
    from tools.group_tools import (
        tableau_add_user_to_group,
        tableau_create_group,
        tableau_list_groups,
        tableau_remove_user_from_group,
    )
    from tools.permission_tools import (
        tableau_grant_permission,
        tableau_list_permissions,
        tableau_revoke_permission,
    )
    from tools.project_tools import (
        tableau_create_project,
        tableau_delete_project,
        tableau_get_project,
        tableau_list_projects,
        tableau_update_project,
    )
    from tools.publish_workbook_tools import tableau_publish_workbook
    from tools.site_tools import tableau_list_sites
    from tools.user_tools import (
        tableau_create_user,
        tableau_delete_user,
        tableau_get_user,
        tableau_list_users,
        tableau_update_user,
    )
    from tools.view_tools import (
        tableau_get_view,
        tableau_get_view_metadata,
        tableau_list_views,
        tableau_query_view_data_to_file,
        tableau_query_view_image,
    )
    from tools.visualization_tools import (
        tableau_configure_shelf,
        tableau_create_sheet,
        tableau_create_visualization,
        tableau_get_sheets,
        tableau_list_fields,
        tableau_upload_csv,
    )
    from tools.workbook_tools import (
        tableau_create_workbook,
        tableau_delete_workbook,
        tableau_get_workbook,
        tableau_list_workbooks,
        tableau_update_workbook,
    )

    # Site tools
    mcp.tool(tableau_list_sites)

    # User tools
    mcp.tool(tableau_create_user)
    mcp.tool(tableau_list_users)
    mcp.tool(tableau_get_user)
    mcp.tool(tableau_update_user)
    mcp.tool(tableau_delete_user)

    # Project tools
    mcp.tool(tableau_create_project)
    mcp.tool(tableau_list_projects)
    mcp.tool(tableau_get_project)
    mcp.tool(tableau_update_project)
    mcp.tool(tableau_delete_project)

    # Workbook tools
    mcp.tool(tableau_create_workbook)
    mcp.tool(tableau_list_workbooks)
    mcp.tool(tableau_get_workbook)
    mcp.tool(tableau_update_workbook)
    mcp.tool(tableau_delete_workbook)
    mcp.tool(tableau_publish_workbook)

    # Connection tools
    mcp.tool(tableau_create_workbook_connection)
    mcp.tool(tableau_list_workbook_connections)
    mcp.tool(tableau_delete_workbook_connection)

    # View tools
    mcp.tool(tableau_list_views)
    mcp.tool(tableau_get_view)
    mcp.tool(tableau_get_view_metadata)
    mcp.tool(tableau_query_view_data_to_file)
    mcp.tool(tableau_query_view_image)

    # Datasource tools
    mcp.tool(tableau_create_datasource)
    mcp.tool(tableau_list_datasources)
    mcp.tool(tableau_get_datasource)
    mcp.tool(tableau_update_datasource)
    mcp.tool(tableau_delete_datasource)

    # Group tools
    mcp.tool(tableau_create_group)
    mcp.tool(tableau_list_groups)
    mcp.tool(tableau_add_user_to_group)
    mcp.tool(tableau_remove_user_from_group)

    # Permission tools
    mcp.tool(tableau_grant_permission)
    mcp.tool(tableau_list_permissions)
    mcp.tool(tableau_revoke_permission)

    # Visualization tools (drag-and-drop query builder)
    mcp.tool(tableau_upload_csv)
    mcp.tool(tableau_get_sheets)
    mcp.tool(tableau_list_fields)
    mcp.tool(tableau_configure_shelf)
    mcp.tool(tableau_create_visualization)
    mcp.tool(tableau_create_sheet)

else:
    # ========================================================================
    # LLM MODE: Consolidated meta-tools for efficient token usage
    # ========================================================================
    # Register meta-tools (consolidates individual tools into domain groups)
    mcp.tool(tableau_admin)
    mcp.tool(tableau_users)
    mcp.tool(tableau_projects)
    mcp.tool(tableau_workbooks)
    mcp.tool(tableau_views)
    mcp.tool(tableau_datasources)
    mcp.tool(tableau_groups)
    mcp.tool(tableau_visualization)
    mcp.tool(tableau_schema)

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
