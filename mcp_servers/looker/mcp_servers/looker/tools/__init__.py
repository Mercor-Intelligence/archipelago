"""Tools package.

All MCP tools are exported as async functions from this package.

Note: V2 tools (tools/v2/) are NOT exported here. They are used internally
by meta-tools but not exposed as standalone MCP tools.

Note: Data layer initialization is handled by the MCP server lifespan in main.py,
not at import time. This prevents initialization during tool discovery.
"""

from tools.content_discovery import (
    _search_content,
    get_dashboard,
    get_look,
    list_dashboards,
    list_explores,
    list_fields,
    list_folders,
    list_looks,
    run_dashboard,
    run_look,
)
from tools.content_management import (
    add_tile_to_dashboard,
    create_dashboard,
    create_look,
)
from tools.health import health_check
from tools.lookml_discovery import get_explore, list_lookml_models
from tools.lookml_management import (
    deploy_lookml,
    generate_lookml,
    get_generated_lookml,
    list_available_views,
)
from tools.query_execution import (
    create_query,
    export_query,
    run_query_by_id,
    run_query_inline,
)
from tools.sql_runner import run_sql_query

__all__ = [
    # LookML Discovery
    "list_lookml_models",
    "get_explore",
    # LookML Management
    "generate_lookml",
    "get_generated_lookml",
    "list_available_views",
    "deploy_lookml",
    # Content Discovery
    "list_folders",
    "list_looks",
    "get_look",
    "run_look",
    "list_dashboards",
    "get_dashboard",
    "run_dashboard",
    "_search_content",
    "list_explores",
    "list_fields",
    # Content Management
    "create_look",
    "create_dashboard",
    "add_tile_to_dashboard",
    # Query Execution
    "create_query",
    "run_query_inline",
    "run_query_by_id",
    "export_query",
    # Utility
    "run_sql_query",
    "health_check",
]
