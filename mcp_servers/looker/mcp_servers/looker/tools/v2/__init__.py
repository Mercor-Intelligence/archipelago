"""V2 Looker Tools - Enhanced Data Science MCP Tools.

This package provides the complete set of Looker API tools for data science workflows:
- Explore discovery and field listing
- Query creation and execution (JSON/CSV export)
- Look creation and management
- Dashboard creation and tile management
- Dashboard rendering (PDF/PNG export)

These tools map directly to Looker API endpoints for a clean-room implementation
that works standalone without requiring actual Looker credentials.
"""

# Models
# Dashboard tools
from .dashboards import (
    looker_delete_dashboard,
    looker_delete_tile,
    looker_reorder_dashboard_tiles,
    looker_search_dashboards,
)

# Explore tools (moved to tools.content_discovery)
# from .explores import looker_list_explores, looker_list_fields
# Look tools
from .looks import (
    looker_delete_look,
    looker_search_looks,
    looker_update_look,
)
from .models import (
    AddTileRequest,
    AddTileResponse,
    CreateDashboardRequest,
    CreateDashboardResponse,
    CreateLookRequest,
    CreateLookResponse,
    DashboardSummary,
    DeleteDashboardRequest,
    DeleteDashboardResponse,
    DeleteLookRequest,
    DeleteLookResponse,
    DeleteTileRequest,
    DeleteTileResponse,
    DownloadRenderedFileRequest,
    DownloadRenderedFileResponse,
    ExportDashboardRequest,
    ExportDashboardResponse,
    FieldInfo,
    GetQueryRequest,
    GetQueryResponse,
    ListExploresRequest,
    ListFieldsRequest,
    ListFieldsResponse,
    LookSummary,
    RenderLookRequest,
    ReorderTilesRequest,
    ReorderTilesResponse,
    RunQueryCsvResponse,
    RunQueryJsonResponse,
    RunQueryRequest,
    SearchDashboardsRequest,
    SearchDashboardsResponse,
    SearchLooksRequest,
    SearchLooksResponse,
    UpdateLookRequest,
    UpdateLookResponse,
)

# Query tools
from .queries import (
    looker_create_query,
    looker_get_query,
    looker_run_query_csv,
    looker_run_query_json,
)

# Rendering tools
from .rendering import (
    looker_download_rendered_file,
    looker_export_dashboard_pdf,
    looker_export_dashboard_png,
    looker_render_look,
)

__all__ = [
    # Models
    "AddTileRequest",
    "AddTileResponse",
    "CreateDashboardRequest",
    "CreateDashboardResponse",
    "CreateLookRequest",
    "CreateLookResponse",
    "DashboardSummary",
    "DeleteDashboardRequest",
    "DeleteDashboardResponse",
    "DeleteLookRequest",
    "DeleteLookResponse",
    "DeleteTileRequest",
    "DeleteTileResponse",
    "DownloadRenderedFileRequest",
    "DownloadRenderedFileResponse",
    "ExportDashboardRequest",
    "ExportDashboardResponse",
    "FieldInfo",
    "GetQueryRequest",
    "GetQueryResponse",
    "ListExploresRequest",
    "ListFieldsRequest",
    "ListFieldsResponse",
    "LookSummary",
    "RenderLookRequest",
    "ReorderTilesRequest",
    "ReorderTilesResponse",
    "RunQueryCsvResponse",
    "RunQueryJsonResponse",
    "RunQueryRequest",
    "SearchDashboardsRequest",
    "SearchDashboardsResponse",
    "SearchLooksRequest",
    "SearchLooksResponse",
    "UpdateLookRequest",
    "UpdateLookResponse",
    # Explore tools (moved to tools.content_discovery)
    # "looker_list_explores",
    # "looker_list_fields",
    # Query tools
    "looker_create_query",
    "looker_get_query",
    "looker_run_query_csv",
    "looker_run_query_json",
    # Look tools
    "looker_delete_look",
    "looker_search_looks",
    "looker_update_look",
    # Dashboard tools
    "looker_delete_dashboard",
    "looker_delete_tile",
    "looker_reorder_dashboard_tiles",
    "looker_search_dashboards",
    # Rendering tools
    "looker_download_rendered_file",
    "looker_export_dashboard_pdf",
    "looker_export_dashboard_png",
    "looker_render_look",
]
