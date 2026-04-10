"""Credit notes resource implementation for online provider."""

from typing import Any

from loguru import logger


async def get_credit_notes(
    self,
    ids: list[str] | None = None,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Fetch credit notes from the live Xero API.

    Args:
        self: Provider instance
        ids: Optional list of credit note IDs to filter by
        where: Optional OData filter expression
        page: Optional page number (1-indexed)

    Returns:
        Dictionary containing credit notes array and metadata

    Reference:
        https://developer.xero.com/documentation/api/accounting/creditnotes
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

    logger.debug(f"Fetching credit notes with params: {params}")
    response, has_next = await self._make_request(
        "/CreditNotes", params=params, return_pagination=True
    )

    # Unified pagination metadata (Xero page size is effectively 100)
    page_size = 100
    current_page = page if page is not None else 1

    response = self._add_metadata(response, "xero-api", "online")
    response["meta"]["page"] = current_page
    response["meta"]["page_size"] = page_size
    response["meta"]["has_next"] = has_next

    return response
