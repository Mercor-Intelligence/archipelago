"""Quotes resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_quotes(
    self,
    ids: list[str] | None = None,
    where: str | None = None,
    page: int | None = None,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch quotes from the live Xero API.

    Args:
        self: Provider instance
        ids: Optional list of quote IDs to filter by
        where: Optional OData filter expression
        page: Optional page number (1-indexed)
        statuses: Optional list of statuses to filter by

    Returns:
        Dictionary containing quotes array and metadata
    """
    params: dict[str, Any] = {}

    if ids:
        params["IDs"] = ",".join(ids)
    if where:
        params["where"] = where
    if page is not None:
        if page < 1:
            raise ValueError("Page number must be >= 1")
        params["page"] = str(page)
    if statuses:
        params["Statuses"] = ",".join(statuses)

    logger.debug(f"Fetching quotes with params: {params}")
    response = await self._make_request("/Quotes", params=params)
    return self._add_metadata(response, "xero-api", "online")
