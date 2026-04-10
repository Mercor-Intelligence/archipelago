"""Assets resource implementation for online provider."""

from __future__ import annotations

from typing import Any


async def get_assets(
    self,
    status: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Retrieve assets from the Xero Assets API."""
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if page is not None:
        params["page"] = page
    if page_size is not None:
        params["pageSize"] = page_size

    response = await self._make_request(
        "/Assets",
        params=params or None,
        base_url=self.config.xero_assets_api_base_url,
    )

    return self._add_metadata(response, "xero-api", "online")


async def get_asset_types(self) -> dict[str, Any]:
    """Retrieve asset types from the Xero Assets API."""
    raw_response = await self._make_request(
        "/AssetTypes",
        base_url=self.config.xero_assets_api_base_url,
    )

    if isinstance(raw_response, list):
        asset_types_list = raw_response
    else:
        asset_types_list = raw_response.get("AssetTypes") or []

    normalized_asset_types = [
        asset.copy() if isinstance(asset, dict) else asset for asset in asset_types_list
    ]

    payload = {"AssetTypes": normalized_asset_types}
    return self._add_metadata(payload, "xero-api", "online")
