"""Prepayments resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_prepayments(
    self,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Get prepayments from the live Xero API.

    Args:
        where: Filter expression (e.g., 'Type=="RECEIVE-PREPAYMENT"')
        page: Page number (1-indexed, must be >= 1)

    Returns:
        Dictionary with ``Prepayments`` array and metadata.

    Reference:
        https://developer.xero.com/documentation/api/accounting/prepayments
    """
    # Build query parameters
    params: dict[str, Any] = {}

    if where:
        params["where"] = where

    if page is not None:
        if page < 1:
            raise ValueError("Page number must be >= 1")
        # Xero API expects page as string (1-indexed)
        params["page"] = str(page)

    logger.debug(f"Fetching prepayments with params: {params}")
    response, has_next = await self._make_request(
        "/Prepayments", params=params, return_pagination=True
    )

    # Unified pagination metadata (Xero page size is effectively 100)
    page_size = 100
    current_page = page if page is not None else 1

    response = self._add_metadata(response, "xero-api", "online")
    response["meta"]["page"] = current_page
    response["meta"]["page_size"] = page_size
    response["meta"]["has_next"] = has_next

    return response
