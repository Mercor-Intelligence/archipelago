"""Associations resource implementation for offline provider."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import Association, File
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.providers.exceptions import (
    XeroApiNotFoundError,
    XeroApiValidationError,
)

# UUID validation pattern
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_valid_uuid(value: str) -> bool:
    """Check if a string is a valid UUID format."""
    return bool(UUID_PATTERN.match(value))


async def get_associations(
    self,
    file_id: str,
) -> dict[str, Any]:
    """Get associations for a specific file.

    Args:
        self: Provider instance
        file_id: File UUID to get associations for (required)

    Returns:
        Dictionary containing associations array and metadata

    Raises:
        XeroApiValidationError: If file_id is not a valid UUID
        XeroApiNotFoundError: If file_id does not exist
    """
    # Validate UUID format
    if not _is_valid_uuid(file_id):
        raise XeroApiValidationError(
            message=f"Invalid file_id format: {file_id}. Must be a valid UUID.",
            status_code=400,
        )

    # Check if file exists
    async with async_session() as session:
        file_result = await session.execute(select(File).where(File.file_id == file_id))
        file_record = file_result.scalars().first()

        if not file_record:
            raise XeroApiNotFoundError(
                message=f"File not found: {file_id}",
                status_code=404,
            )

        # Get associations for this file
        result = await session.execute(select(Association).where(Association.file_id == file_id))
        associations = result.scalars().all()

    associations_data = [assoc.to_dict() for assoc in associations]

    response: dict[str, Any] = {"Associations": associations_data}
    return self._add_metadata(response, "xero-mock", "offline")
