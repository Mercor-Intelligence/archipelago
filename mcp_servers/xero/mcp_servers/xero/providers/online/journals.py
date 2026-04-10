"""Journals resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_journals(
    self,
    offset: int | None = None,
    payments_only: bool | None = None,
) -> dict[str, Any]:
    """Fetch journals from the live Xero API."""
    params: dict[str, Any] = {}

    if offset is not None:
        params["offset"] = str(offset)

    if payments_only is not None:
        params["paymentsOnly"] = str(payments_only).lower()

    logger.debug(f"Fetching journals with params: {params}")
    response = await self._make_request("/Journals", params=params)
    return self._add_metadata(response, "xero-api", "online")
