"""Offline provider implementation for Xero Files folders."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import Folder
from mcp_servers.xero.db.session import async_session


async def get_folders(self) -> dict[str, Any]:
    """Get folder metadata from database."""
    async with async_session() as session:
        result = await session.execute(select(Folder))
        folders = result.scalars().all()

    response = {"Folders": [folder.to_dict() for folder in folders]}
    return self._add_metadata(response, "xero-mock", "offline")
