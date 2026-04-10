"""Budgets resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_budgets(self) -> dict[str, Any]:
    """Get budget entities from the live Xero API.

    Returns:
        Dictionary containing ``Budgets`` array and metadata.

    Reference: https://developer.xero.com/documentation/api/accounting/budgets
    """
    logger.debug("Fetching budgets")
    response = await self._make_request("/Budgets")
    return self._add_metadata(response, "xero-api", "online")
