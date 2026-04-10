"""Accounts resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_accounts(
    self, where: str | None = None, order: str | None = None, page: int | None = None
) -> dict[str, Any]:
    """Get chart of accounts from the live Xero API.

    Args:
        where: Optional filter expression (e.g., "Status==\"ACTIVE\"")
        order: Optional ordering expression (e.g., "Code ASC")
        page: Optional 1-indexed page number for pagination

    Returns:
        Dictionary containing ``Accounts`` array and metadata.

    Reference: https://developer.xero.com/documentation/api/accounting/accounts
    """
    params: dict[str, Any] = {}

    if where:
        params["where"] = where

    if order:
        params["order"] = order

    if page is not None:
        if page < 1:
            raise ValueError("Page number must be >= 1")
        params["page"] = str(page)

    logger.debug(f"Fetching accounts with params: {params}")
    response = await self._make_request("/Accounts", params=params)
    return self._add_metadata(response, "xero-api", "online")
