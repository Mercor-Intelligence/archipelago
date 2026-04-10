"""Explore discovery tools for V2 Looker API."""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from models import ExploreRequest, ExploreResponse
from repository_factory import create_repository

from .models import FieldInfo, ListFieldsRequest, ListFieldsResponse


async def looker_list_explores(model: str) -> dict:
    """List available explores for a model."""
    from models import LookMLModelRequest, LookMLModelResponse

    repo = create_repository(LookMLModelRequest, LookMLModelResponse)
    response = await repo.get(LookMLModelRequest())

    if response is None:
        return {"model": model, "explores": []}

    # Find the requested model
    for m in response.models:
        if m.name == model:
            return {
                "model": model,
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

    return {"model": model, "explores": [], "error": f"Model '{model}' not found"}


async def looker_list_fields(request: ListFieldsRequest) -> ListFieldsResponse:
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
