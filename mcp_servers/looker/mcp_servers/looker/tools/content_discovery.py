"""Content discovery tools.

Nice-to-have tools for discovering existing Looks, Dashboards, and folders.

This module provides backward-compatible wrapper functions for tests.
The actual tool logic is implemented in repository classes.
"""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (
    ExploreRequest,
    ExploreResponse,
    GetDashboardRequest,
    GetDashboardResponse,
    GetLookRequest,
    GetLookResponse,
    ListDashboardsRequest,
    ListDashboardsResponse,
    ListFoldersRequest,
    ListFoldersResponse,
    ListLooksRequest,
    ListLooksResponse,
    LookMLModelRequest,
    LookMLModelResponse,
    RunDashboardRequest,
    RunDashboardResponse,
    RunLookRequest,
    RunLookResponse,
    SearchContentRequest,
    SearchContentResponse,
)
from repository_factory import create_repository

from tools.v2.models import FieldInfo, ListExploresRequest, ListFieldsRequest, ListFieldsResponse


async def list_folders(request: ListFoldersRequest) -> ListFoldersResponse:
    """List all folders containing Looks and Dashboards."""
    repo = create_repository(ListFoldersRequest, ListFoldersResponse)
    response = await repo.get(request)
    if response is None:
        return ListFoldersResponse(folders=[])
    return response


async def list_looks(request: ListLooksRequest) -> ListLooksResponse:
    """List saved Looks (query visualizations)."""
    repo = create_repository(ListLooksRequest, ListLooksResponse)
    response = await repo.get(request)
    if response is None:
        return ListLooksResponse(looks=[])
    return response


async def get_look(request: GetLookRequest) -> GetLookResponse:
    """Get a specific Look by ID with its query configuration and metadata."""
    repo = create_repository(GetLookRequest, GetLookResponse)
    response = await repo.get(request)
    if response is None:
        raise ValueError(f"Look {request.look_id} not found")
    return response


async def run_look(request: RunLookRequest) -> RunLookResponse:
    """Execute a saved Look by ID and return query results."""
    repo = create_repository(RunLookRequest, RunLookResponse)
    response = await repo.get(request)
    if response is None:
        raise ValueError(f"Look {request.look_id} not found")
    return response


async def _search_content(request: SearchContentRequest) -> SearchContentResponse:
    """Search for content (Looks, Dashboards) by text query."""
    repo = create_repository(SearchContentRequest, SearchContentResponse)
    response = await repo.get(request)
    if response is None:
        return SearchContentResponse(results=[], total=0)
    return response


async def list_dashboards(request: ListDashboardsRequest) -> ListDashboardsResponse:
    """List all available dashboards with optional filtering."""
    repo = create_repository(ListDashboardsRequest, ListDashboardsResponse)
    response = await repo.get(request)
    if response is None:
        return ListDashboardsResponse(dashboards=[], total_count=0)
    return response


async def get_dashboard(request: GetDashboardRequest) -> GetDashboardResponse:
    """Get a specific dashboard by ID with full tile definitions."""
    repo = create_repository(GetDashboardRequest, GetDashboardResponse)
    response = await repo.get(request)
    if response is None:
        raise ValueError(f"Dashboard {request.dashboard_id} not found")
    return response


async def run_dashboard(request: RunDashboardRequest) -> RunDashboardResponse:
    """Execute all tile queries for a dashboard and return results."""
    repo = create_repository(RunDashboardRequest, RunDashboardResponse)
    response = await repo.get(request)
    if response is None:
        raise ValueError(f"Dashboard {request.dashboard_id} not found")
    return response


async def list_explores(request: ListExploresRequest) -> dict:
    """List available explores for a model."""
    repo = create_repository(LookMLModelRequest, LookMLModelResponse)
    response = await repo.get(LookMLModelRequest())

    if response is None:
        return {"model": request.model, "explores": []}

    # Find the requested model
    for m in response.models:
        if m.name == request.model:
            return {
                "model": request.model,
                "explores": [
                    {
                        "name": e.name,
                        "label": e.label,
                        "description": e.description,
                        "hidden": e.hidden,
                        "group_label": e.group_label,
                    }
                    for e in m.explores
                ],
            }

    return {"model": request.model, "explores": [], "error": f"Model '{request.model}' not found"}


async def list_fields(request: ListFieldsRequest) -> ListFieldsResponse:
    """List fields (dimensions and measures) for an explore."""
    repo = create_repository(ExploreRequest, ExploreResponse)
    explore_response = await repo.get(ExploreRequest(model=request.model, explore=request.explore))

    if explore_response is None:
        return ListFieldsResponse(
            model=request.model,
            explore=request.explore,
            dimensions=[],
            measures=[],
        )

    dimensions = [
        FieldInfo(
            name=d.name,
            label=d.label,
            type=d.type if isinstance(d.type, str) else d.type.value,
            description=d.description,
        )
        for d in explore_response.fields.dimensions
    ]

    measures = [
        FieldInfo(
            name=m.name,
            label=m.label,
            type=m.type if isinstance(m.type, str) else m.type.value,
            description=m.description,
        )
        for m in explore_response.fields.measures
    ]

    return ListFieldsResponse(
        model=request.model,
        explore=request.explore,
        dimensions=dimensions,
        measures=measures,
    )
