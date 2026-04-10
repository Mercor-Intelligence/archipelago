"""Overpayments resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_overpayments(
    self,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Fetch overpayments from the live Xero API."""
    params: dict[str, Any] = {}

    if where:
        params["where"] = where
    if page is not None:
        if page < 1:
            raise ValueError("Page number must be >= 1")
        params["page"] = str(page)

    logger.debug(f"Fetching overpayments with params: {params}")
    response = await self._make_request("/Overpayments", params=params)
    return self._add_metadata(response, "xero-api", "online")
