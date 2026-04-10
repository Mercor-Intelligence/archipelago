"""Look management tools for V2 Looker API."""

import sys
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import settings
from loguru import logger

from .models import (
    DeleteLookRequest,
    DeleteLookResponse,
    SearchLooksRequest,
    SearchLooksResponse,
    UpdateLookRequest,
    UpdateLookResponse,
)


async def looker_update_look(request: UpdateLookRequest) -> UpdateLookResponse:
    """Update an existing Look."""
    if settings.is_offline_mode():
        from store_accessors import update_look

        look = update_look(
            request.look_id,
            title=request.title,
            description=request.description,
            query_id=request.query_id,
        )
        if look:
            logger.info(f"Updated look {request.look_id} in offline store")

    return UpdateLookResponse(
        look_id=request.look_id,
        title=request.title or "Updated Look",
        updated=True,
    )


async def looker_delete_look(request: DeleteLookRequest) -> DeleteLookResponse:
    """Delete a Look."""
    if settings.is_offline_mode():
        from store_accessors import delete_look

        delete_look(request.look_id)
        logger.info(f"Deleted look {request.look_id} from offline store")

    return DeleteLookResponse(
        look_id=request.look_id,
        deleted=True,
    )


async def looker_search_looks(request: SearchLooksRequest) -> SearchLooksResponse:
    """Search for Looks by title or folder."""
    if settings.is_offline_mode():
        from store_accessors import get_all_looks

        # Collect all looks using unified accessor
        all_looks = list(get_all_looks())

        # Filter by title (case-insensitive contains)
        if request.title:
            all_looks = [look for look in all_looks if request.title.lower() in look.title.lower()]

        # Filter by folder
        if request.folder_id:
            all_looks = [look for look in all_looks if look.folder_id == request.folder_id]

        # Apply limit
        limit = request.limit or 50
        total = len(all_looks)
        all_looks = all_looks[:limit]

        # Convert to response format (LookSummary model)
        look_infos = [
            {
                "look_id": str(look.id),
                "title": look.title,
                "folder_id": look.folder_id,
                "description": getattr(look, "description", None),
            }
            for look in all_looks
        ]

        return SearchLooksResponse(
            looks=look_infos,
            total_count=total,
        )

    # In live mode, this would query the Looker API
    return SearchLooksResponse(
        looks=[],
        total_count=0,
    )
