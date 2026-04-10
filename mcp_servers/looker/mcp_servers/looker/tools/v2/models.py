"""Pydantic models for V2 Looker tools.

All request/response models used by V2 tools are defined here.
"""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field


# =============================================================================
# Explore Discovery
# =============================================================================
class ListExploresRequest(BaseModel):
    """Request to list explores for a model."""

    model: str = Field(..., description="LookML model name")


# =============================================================================
# Field Listing
# =============================================================================
class ListFieldsRequest(BaseModel):
    """Request to list fields (dimensions, measures) for an explore."""

    model: str = Field(..., description="LookML model name")
    explore: str = Field(..., description="Explore name")


class FieldInfo(BaseModel):
    """Information about a single field."""

    model_config = ConfigDict(extra="ignore")  # Ignore all other API fields

    name: str = Field(..., description="Field name (e.g., 'orders.total_revenue')")
    label: str | None = Field(None, description="Human-readable label")
    type: str = Field(..., description="Field type (string, number, datetime, etc.)")
    description: str | None = Field(None, description="Field description")


class ListFieldsResponse(BaseModel):
    """Response containing fields for an explore."""

    model: str = Field(..., description="LookML model name")
    explore: str = Field(..., description="Explore name")
    dimensions: list[FieldInfo] = Field(default_factory=list)
    measures: list[FieldInfo] = Field(default_factory=list)


# =============================================================================
# Query Execution
# =============================================================================
class RunQueryRequest(BaseModel):
    """Request to run a query by ID."""

    query_id: str = Field(..., description="Query ID to run")
    limit: int | None = Field(None, description="Row limit")


class RunQueryJsonResponse(BaseModel):
    """JSON query results."""

    query_id: str = Field(..., description="Query ID")
    data: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = Field(0, description="Number of rows returned")
    sql: str | None = Field(None, description="Generated SQL")


class RunQueryCsvResponse(BaseModel):
    """CSV query results."""

    query_id: str = Field(..., description="Query ID")
    csv_data: str = Field(..., description="CSV formatted data")
    row_count: int = Field(0, description="Number of rows returned")


class GetQueryRequest(BaseModel):
    """Request to get a saved query definition."""

    query_id: str = Field(..., description="Query ID to retrieve")


class GetQueryResponse(BaseModel):
    """Response with query definition."""

    query_id: str = Field(..., description="Query ID")
    model: str | None = Field(None, description="LookML model name")
    view: str | None = Field(None, description="View/explore name")
    fields: list[str] = Field(default_factory=list, description="Selected fields")
    filters: dict[str, list[str]] = Field(
        default_factory=dict, description="Applied filters (supports multiple values per field)"
    )
    sorts: list[str] = Field(default_factory=list, description="Sort order")
    limit: int | None = Field(None, description="Row limit")
    sql: str | None = Field(None, description="Generated SQL")


# =============================================================================
# Look Management
# =============================================================================
class CreateLookRequest(BaseModel):
    """Request to create a new Look."""

    title: str = Field(..., description="Look title")
    query_id: str = Field(..., description="Query ID for the Look")
    folder_id: str = Field(..., description="Folder to save the Look in")
    description: str | None = Field(None, description="Look description")
    vis_config: dict | None = Field(
        None,
        description="Visualization config (e.g., {'type': 'looker_pie'}). "
        "Chart types: looker_column, looker_bar, looker_line, looker_pie, "
        "looker_area, looker_scatter, single_value, table",
    )


class CreateLookResponse(BaseModel):
    """Response after creating a Look."""

    look_id: str = Field(..., description="Created Look ID")
    title: str = Field(..., description="Look title")
    url: str | None = Field(None, description="URL to view the Look")


class UpdateLookRequest(BaseModel):
    """Request to update an existing Look."""

    look_id: str = Field(..., description="Look ID to update")
    title: str | None = Field(None, description="New title")
    description: str | None = Field(None, description="New description")
    query_id: str | None = Field(None, description="New query ID")


class UpdateLookResponse(BaseModel):
    """Response after updating a Look."""

    look_id: str = Field(..., description="Updated Look ID")
    title: str = Field(..., description="Look title")
    updated: bool = Field(True, description="Whether update succeeded")


class DeleteLookRequest(BaseModel):
    """Request to delete a Look."""

    look_id: str = Field(..., description="Look ID to delete")


class DeleteLookResponse(BaseModel):
    """Response after deleting a Look."""

    look_id: str = Field(..., description="Deleted Look ID")
    deleted: bool = Field(True, description="Whether deletion succeeded")


class SearchLooksRequest(BaseModel):
    """Request to search for Looks."""

    title: str | None = Field(None, description="Filter by title (contains)")
    folder_id: str | None = Field(None, description="Filter by folder ID")
    limit: int = Field(50, description="Maximum results to return")


class LookSummary(BaseModel):
    """Summary information about a Look."""

    look_id: str = Field(..., description="Look ID")
    title: str = Field(..., description="Look title")
    folder_id: str | None = Field(None, description="Folder ID")
    description: str | None = Field(None, description="Look description")
    url: str | None = Field(None, description="URL to view the Look")


class SearchLooksResponse(BaseModel):
    """Response containing search results."""

    looks: list[LookSummary] = Field(default_factory=list)
    total_count: int = Field(0, description="Total number of matching looks")


class RenderLookRequest(BaseModel):
    """Request to render a Look as PDF or PNG."""

    look_id: str = Field(..., description="Look ID to render")
    format: str = Field("png", description="Output format (pdf or png)")
    width: int | None = Field(None, description="Width in pixels")
    height: int | None = Field(None, description="Height in pixels")


# =============================================================================
# Dashboard Management
# =============================================================================
class CreateDashboardRequest(BaseModel):
    """Request to create a new Dashboard."""

    title: str = Field(..., description="Dashboard title")
    folder_id: str = Field(..., description="Folder to save the Dashboard in (required)")
    description: str | None = Field(None, description="Dashboard description")


class CreateDashboardResponse(BaseModel):
    """Response after creating a Dashboard."""

    dashboard_id: str = Field(..., description="Created Dashboard ID")
    title: str = Field(..., description="Dashboard title")
    url: str | None = Field(None, description="URL to view the Dashboard")


class AddTileRequest(BaseModel):
    """Request to add a tile to a Dashboard."""

    dashboard_id: str = Field(..., description="Dashboard ID")
    query_id: str | None = Field(None, description="Query ID for the tile")
    look_id: str | None = Field(None, description="Look ID for the tile (alternative to query_id)")
    title: str | None = Field(None, description="Tile title")
    type: str = Field("vis", description="Tile type (vis, text, etc.)")
    chart_type: str | None = Field(
        None,
        description="Chart visualization type (column, bar, line, pie, area, table). "
        "Defaults to 'column' for vertical bar charts.",
    )


class AddTileResponse(BaseModel):
    """Response after adding a tile."""

    dashboard_element_id: str = Field(..., description="Created tile/element ID")
    dashboard_id: str = Field(..., description="Dashboard ID")


class ReorderTilesRequest(BaseModel):
    """Request to reorder dashboard tiles."""

    dashboard_id: str = Field(..., description="Dashboard ID")
    tile_order: list[str] = Field(..., description="Ordered list of tile/element IDs")


class ReorderTilesResponse(BaseModel):
    """Response after reordering tiles."""

    dashboard_id: str = Field(..., description="Dashboard ID")
    success: bool = Field(True, description="Whether reorder succeeded")


class DeleteTileRequest(BaseModel):
    """Request to delete a dashboard tile."""

    dashboard_element_id: str = Field(..., description="Tile/element ID to delete")


class DeleteTileResponse(BaseModel):
    """Response after deleting a tile."""

    dashboard_element_id: str = Field(..., description="Deleted tile ID")
    deleted: bool = Field(True, description="Whether deletion succeeded")


class DeleteDashboardRequest(BaseModel):
    """Request to delete a Dashboard."""

    dashboard_id: str = Field(..., description="Dashboard ID to delete")


class DeleteDashboardResponse(BaseModel):
    """Response after deleting a Dashboard."""

    dashboard_id: str = Field(..., description="Deleted Dashboard ID")
    deleted: bool = Field(True, description="Whether deletion succeeded")


class SearchDashboardsRequest(BaseModel):
    """Request to search for Dashboards."""

    title: str | None = Field(None, description="Filter by title (contains)")
    folder_id: str | None = Field(None, description="Filter by folder ID")
    limit: int = Field(50, description="Maximum results to return")


class DashboardSummary(BaseModel):
    """Summary information about a Dashboard."""

    dashboard_id: str = Field(..., description="Dashboard ID")
    title: str = Field(..., description="Dashboard title")
    folder_id: str | None = Field(None, description="Folder ID")
    description: str | None = Field(None, description="Dashboard description")
    url: str | None = Field(None, description="URL to view the Dashboard")


class SearchDashboardsResponse(BaseModel):
    """Response containing search results."""

    dashboards: list[DashboardSummary] = Field(default_factory=list)
    total_count: int = Field(0, description="Total number of matching dashboards")


# =============================================================================
# Rendering
# =============================================================================
class ExportDashboardRequest(BaseModel):
    """Request to export a dashboard as PDF or PNG."""

    dashboard_id: str = Field(..., description="Dashboard ID to export")
    format: str = Field("pdf", description="Export format (pdf or png)")
    width: int | None = Field(None, description="Width in pixels")
    height: int | None = Field(None, description="Height in pixels")


class ExportDashboardResponse(BaseModel):
    """Response with render task information."""

    render_task_id: str = Field(..., description="Render task ID")
    dashboard_id: str = Field(..., description="Dashboard ID")
    format: str = Field(..., description="Export format")
    status: str = Field("pending", description="Task status")


class DownloadRenderedFileRequest(BaseModel):
    """Request to download a rendered file."""

    render_task_id: str = Field(..., description="Render task ID")


class DownloadRenderedFileResponse(BaseModel):
    """Response with rendered file data."""

    render_task_id: str = Field(..., description="Render task ID")
    status: str = Field(..., description="Task status (success, failure, pending)")
    content_type: str | None = Field(None, description="MIME type of the file")
    file_data: bytes | None = Field(None, description="Binary file data (base64 encoded)")
    file_size: int | None = Field(None, description="File size in bytes")
