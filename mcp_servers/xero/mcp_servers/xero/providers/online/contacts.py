"""Contacts resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_contacts(
    self,
    ids: list[str] | None = None,
    where: str | None = None,
    include_archived: bool = False,
    page: int | None = None,
) -> dict[str, Any]:
    """
    Get contacts (customers/suppliers) from Xero API.

    Args:
        ids: Optional list of contact IDs to filter by
        where: Optional filter expression (e.g., 'IsCustomer==true')
        include_archived: Whether to include archived contacts
        page: Optional page number for pagination (1-indexed)

    Returns:
        Dictionary containing Contacts array and metadata

    Reference: https://developer.xero.com/documentation/api/accounting/contacts
    """
    # Build query parameters
    params = {}

    if ids:
        # Xero API expects comma-separated IDs in IDs parameter
        params["IDs"] = ",".join(ids)

    if where:
        params["where"] = where

    if include_archived:
        params["includeArchived"] = "true"

    if page is not None:
        params["page"] = str(page)

    # Make request to Xero API
    logger.debug(f"Fetching contacts with params: {params}")
    response = await self._make_request("/Contacts", params=params)

    # Add metadata for audit trail
    return self._add_metadata(response, "xero-api", "online")
