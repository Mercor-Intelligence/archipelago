"""Associations resource implementation for online provider."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from mcp_servers.xero.providers.exceptions import XeroApiValidationError

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
    """Fetch associations for a file from the live Xero Files API.

    Args:
        self: Provider instance
        file_id: File UUID to get associations for (required)

    Returns:
        Dictionary containing associations array and metadata

    Raises:
        XeroApiValidationError: If file_id is not a valid UUID
    """
    # Validate UUID format (consistent with offline provider)
    if not _is_valid_uuid(file_id):
        raise XeroApiValidationError(
            message=f"Invalid file_id format: {file_id}. Must be a valid UUID.",
            status_code=400,
        )

    logger.debug(f"Fetching associations for file: {file_id}")

    # Xero Files API: GET /files.xro/1.0/Files/{FileId}/Associations
    raw_response = await self._make_request(
        f"/Files/{file_id}/Associations",
        base_url=self.config.xero_files_api_base_url,
    )

    # The API may return a direct array or wrapped response
    if isinstance(raw_response, list):
        associations_list = raw_response
    else:
        associations_list = (
            raw_response.get("Associations") or raw_response.get("associations") or []
        )

    response: dict[str, Any] = {"Associations": associations_list}
    return self._add_metadata(response, "xero-api", "online")
