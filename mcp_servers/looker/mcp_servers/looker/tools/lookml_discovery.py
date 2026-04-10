"""LookML discovery tools.

Core tools for exploring LookML models and explores.

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
    ListViewsRequest,
    ListViewsResponse,
    LookMLModelRequest,
    LookMLModelResponse,
    View,
)
from repository_factory import create_repository


async def list_lookml_models(request: LookMLModelRequest) -> LookMLModelResponse:
    """List all available LookML models."""
    repo = create_repository(LookMLModelRequest, LookMLModelResponse)
    response = await repo.get(request)
    if response is None:
        return LookMLModelResponse(models=[])
    return response


async def get_explore(request: ExploreRequest) -> ExploreResponse:
    """Get detailed information about a specific Explore."""
    repo = create_repository(ExploreRequest, ExploreResponse)
    response = await repo.get(request)
    if response is None:
        raise ValueError(
            f"Explore '{request.explore}' not found in model '{request.model}'. "
            f"Use list_lookml_models first to discover available explores."
        )
    return response


async def list_views(request: ListViewsRequest) -> ListViewsResponse:
    """List all views (tables) within an Explore, including base and joined views."""
    # First, get the explore to extract view information
    explore_request = ExploreRequest(model=request.model, explore=request.explore)
    explore = await get_explore(explore_request)

    # Build a map of views with their field counts
    view_data: dict[str, dict] = {}

    # Add base view
    base_view_name = explore.view_name
    view_data[base_view_name] = {
        "name": base_view_name,
        "label": base_view_name.replace("_", " ").title(),
        "type": "base",
        "join_type": None,
        "join_on": None,
        "dimension_count": 0,
        "measure_count": 0,
    }

    # Build join lookup for metadata
    join_lookup = {join.name: join for join in explore.joins}

    # Count dimensions by view
    for dim in explore.fields.dimensions:
        view_name = dim.view
        if view_name not in view_data:
            # This is a joined view discovered through fields
            join_info = join_lookup.get(view_name)
            view_data[view_name] = {
                "name": view_name,
                "label": view_name.replace("_", " ").title(),
                "type": "joined",
                "join_type": join_info.type if join_info else None,
                "join_on": join_info.sql_on if join_info else None,
                "dimension_count": 0,
                "measure_count": 0,
            }
        view_data[view_name]["dimension_count"] += 1

    # Count measures by view
    for measure in explore.fields.measures:
        view_name = measure.view
        if view_name not in view_data:
            join_info = join_lookup.get(view_name)
            view_data[view_name] = {
                "name": view_name,
                "label": view_name.replace("_", " ").title(),
                "type": "joined",
                "join_type": join_info.type if join_info else None,
                "join_on": join_info.sql_on if join_info else None,
                "dimension_count": 0,
                "measure_count": 0,
            }
        view_data[view_name]["measure_count"] += 1

    # Also add any joined views that might not have fields yet
    for join in explore.joins:
        if join.name not in view_data:
            view_data[join.name] = {
                "name": join.name,
                "label": join.name.replace("_", " ").title(),
                "type": "joined",
                "join_type": join.type,
                "join_on": join.sql_on,
                "dimension_count": 0,
                "measure_count": 0,
            }

    # Convert to View objects
    views = []
    for view_info in view_data.values():
        views.append(
            View(
                name=view_info["name"],
                label=view_info["label"],
                type=view_info["type"],
                join_type=view_info["join_type"],
                join_on=view_info["join_on"],
                dimension_count=view_info["dimension_count"],
                measure_count=view_info["measure_count"],
                field_count=view_info["dimension_count"] + view_info["measure_count"],
            )
        )

    # Sort: base view first, then joined views alphabetically
    views.sort(key=lambda v: (0 if v.type == "base" else 1, v.name))

    return ListViewsResponse(
        views=views,
        total_count=len(views),
        model_name=request.model,
        explore_name=request.explore,
    )
