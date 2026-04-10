"""Bank transfers resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_bank_transfers(
    self,
    where: str | None = None,
) -> dict[str, Any]:
    """Fetch bank transfers from the live Xero API.

    Args:
        where: Filter expression for the Xero API

    Returns:
        Dictionary with BankTransfers array and metadata
    """
    params: dict[str, Any] = {}

    if where:
        params["where"] = where

    logger.debug(f"Fetching bank transfers with params: {params}")
    response = await self._make_request("/BankTransfers", params=params)
    return self._add_metadata(response, "xero-api", "online")
