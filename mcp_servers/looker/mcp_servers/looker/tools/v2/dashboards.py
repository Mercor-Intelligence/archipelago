"""Dashboard management tools for V2 Looker API."""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import settings
from loguru import logger

from .models import (
    DeleteDashboardRequest,
    DeleteDashboardResponse,
    DeleteTileRequest,
    DeleteTileResponse,
    ReorderTilesRequest,
    ReorderTilesResponse,
    SearchDashboardsRequest,
    SearchDashboardsResponse,
)


async def looker_reorder_dashboard_tiles(
    request: ReorderTilesRequest,
) -> ReorderTilesResponse:
    """Reorder tiles on a Dashboard."""
    return ReorderTilesResponse(
        dashboard_id=request.dashboard_id,
        success=True,
    )


async def looker_delete_tile(request: DeleteTileRequest) -> DeleteTileResponse:
    """Delete a tile from a Dashboard."""
    if settings.is_offline_mode():
        from store_accessors import delete_tile

        delete_tile(request.dashboard_element_id)
        logger.info(f"Deleted tile {request.dashboard_element_id} from offline store")

    return DeleteTileResponse(
        dashboard_element_id=request.dashboard_element_id,
        deleted=True,
    )


async def looker_delete_dashboard(
    request: DeleteDashboardRequest,
) -> DeleteDashboardResponse:
    """Delete a Dashboard."""
    if settings.is_offline_mode():
        from store_accessors import delete_dashboard

        delete_dashboard(request.dashboard_id)
        logger.info(f"Deleted dashboard {request.dashboard_id} from offline store")

    return DeleteDashboardResponse(
        dashboard_id=request.dashboard_id,
        deleted=True,
    )


async def looker_search_dashboards(
    request: SearchDashboardsRequest,
) -> SearchDashboardsResponse:
    """Search for Dashboards by title or folder."""
    if settings.is_offline_mode():
        from store_accessors import get_all_dashboards

        # Collect all dashboards using unified accessor
        all_dashboards = [
            {
                "dashboard_id": str(d["id"]),
                "title": d["title"],
                "folder_id": d["folder_id"],
                "description": d["description"],
            }
            for d in get_all_dashboards()
        ]

        # Filter by title (case-insensitive contains)
        if request.title:
            all_dashboards = [
                d for d in all_dashboards if request.title.lower() in (d.get("title") or "").lower()
            ]

        # Filter by folder
        if request.folder_id:
            all_dashboards = [d for d in all_dashboards if d.get("folder_id") == request.folder_id]

        # Apply limit
        limit = request.limit or 50
        total = len(all_dashboards)
        all_dashboards = all_dashboards[:limit]

        return SearchDashboardsResponse(
            dashboards=all_dashboards,
            total_count=total,
        )

    # In live mode, this would query the Looker API
    return SearchDashboardsResponse(
        dashboards=[],
        total_count=0,
    )
