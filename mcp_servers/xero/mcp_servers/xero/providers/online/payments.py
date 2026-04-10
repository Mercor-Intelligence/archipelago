"""Payments resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_payments(self, where: str | None = None, page: int | None = None) -> dict[str, Any]:
    """Get payments from the live Xero API.

    Args:
        where: Optional filter expression (e.g., 'Status=="AUTHORISED"')
        page: Optional page number for pagination (1-indexed)

    Returns:
        Dictionary containing Payments array and metadata

    Reference: https://developer.xero.com/documentation/api/accounting/payments
    """
    # Build query parameters
    params: dict[str, Any] = {}

    if where:
        params["where"] = where

    if page is not None:
        if page < 1:
            raise ValueError("Page number must be >= 1")
        params["page"] = str(page)

    # Make request to Xero API
    logger.debug(f"Fetching payments with params: {params}")
    response = await self._make_request("/Payments", params=params)

    # Add metadata for audit trail
    return self._add_metadata(response, "xero-api", "online")
