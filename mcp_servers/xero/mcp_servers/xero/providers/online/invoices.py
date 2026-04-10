"""Invoices resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_invoices(
    self,
    ids: list[str] | None = None,
    statuses: list[str] | None = None,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """
    Get invoices (AR/AP) from Xero API.

    Args:
        ids: Optional list of invoice IDs to filter by
        statuses: Optional list of statuses (e.g., ['DRAFT', 'AUTHORISED'])
        where: Optional filter expression
        page: Optional page number for pagination (1-indexed)

    Returns:
        Dictionary containing Invoices array and metadata

    Reference: https://developer.xero.com/documentation/api/accounting/invoices
    """
    # Build query parameters
    params = {}

    if ids:
        # Xero API expects comma-separated IDs in IDs parameter
        params["IDs"] = ",".join(ids)

    if statuses:
        # Xero API expects comma-separated statuses in Statuses parameter
        params["Statuses"] = ",".join(statuses)

    if where:
        params["where"] = where

    # page is guaranteed to be >= 1 by validation in xero_tools.py
    params["page"] = str(page)

    # Make request to Xero API
    logger.debug(f"Fetching invoices with params: {params}")
    response = await self._make_request("/Invoices", params=params)

    # Add metadata for audit trail
    return self._add_metadata(response, "xero-api", "online")
