"""Assets resource implementation for offline provider."""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import func, select

from mcp_servers.xero.db.models import Asset, AssetType
from mcp_servers.xero.db.session import async_session


async def get_assets(
    self,
    status: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Get fixed assets from database with pagination."""
    async with async_session() as session:
        query = select(Asset)

        VALID_STATUSES = {"DRAFT", "REGISTERED", "DISPOSED"}
        if status:
            status_upper = status.upper()
            if status_upper not in VALID_STATUSES:
                raise ValueError(
                    f"Invalid status value: '{status}'. Valid options are: Draft, Registered, Disposed"
                )
            query = query.where(func.upper(Asset.asset_status) == status_upper)

        result = await session.execute(query)
        assets = result.scalars().all()

    # Convert to dict format
    assets_data = [asset.to_dict() for asset in assets]

    total_items = len(assets_data)
    page_number = page if page and page >= 1 else 1
    page_size_value = page_size if page_size and page_size >= 1 else 20

    start = (page_number - 1) * page_size_value
    end = start + page_size_value
    paged_assets = assets_data[start:end]

    page_count = math.ceil(total_items / page_size_value) if total_items else 0

    response = {
        "items": paged_assets,
        "pagination": {
            "page": page_number,
            "pageSize": page_size_value,
            "itemCount": total_items,
            "pageCount": page_count,
        },
    }

    return self._add_metadata(response, "xero-mock", "offline")


async def get_asset_types(self) -> dict[str, Any]:
    """Get asset types with depreciation settings from database."""
    async with async_session() as session:
        result = await session.execute(select(AssetType))
        asset_types = result.scalars().all()

    # Convert to dict format
    asset_types_data = [at.to_dict() for at in asset_types]
    response = {"AssetTypes": asset_types_data}
    return self._add_metadata(response, "xero-mock", "offline")
