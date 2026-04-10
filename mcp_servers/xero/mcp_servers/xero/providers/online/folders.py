"""Folders resource implementation for online provider."""

from __future__ import annotations

from typing import Any


async def get_folders(self) -> dict[str, Any]:
    """Retrieve folder metadata from the live Xero Files API."""
    raw_response = await self._make_request(
        "/Folders",
        base_url=self.config.xero_files_api_base_url,
    )

    if isinstance(raw_response, list):
        folders_list = raw_response
    else:
        folders_list = raw_response.get("Folders") or raw_response.get("folders") or []

    response = {"Folders": folders_list}
    return self._add_metadata(response, "xero-api", "online")
