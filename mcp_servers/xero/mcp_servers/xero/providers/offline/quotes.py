"""Quotes resource implementation for offline provider."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import Quote
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.utils import apply_where_filter, validate_where_clause


async def get_quotes(
    self,
    ids: list[str] | None = None,
    where: str | None = None,
    page: int | None = None,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    """Get sales quotes/estimates with optional filtering and pagination.

    Args:
        self: Provider instance
        ids: Optional list of quote IDs to filter by
        where: Optional OData filter expression
        page: Optional page number (1-indexed)
        statuses: Optional list of statuses to filter by

    Returns:
        Dictionary containing quotes array and metadata
    """
    async with async_session() as session:
        result = await session.execute(select(Quote))
        quotes = result.scalars().all()

    quotes_data = [quote.to_dict() for quote in quotes]

    # Filter by IDs if provided
    if ids:
        quotes_data = [q for q in quotes_data if q.get("QuoteID") in ids]

    # Filter by statuses if provided
    if statuses:
        quotes_data = [q for q in quotes_data if q.get("Status") in statuses]

    # Apply where filter if provided
    if where:
        # Try validation first, but still attempt filtering even if validation fails
        try:
            validate_where_clause(where)
        except ValueError as exc:
            logger.warning(
                f"Where clause validation failed for '{where}': {exc}, attempting filter anyway"
            )

        # Attempt to apply the filter regardless of validation result
        try:
            quotes_data = apply_where_filter(quotes_data, where)
        except ValueError as exc:
            logger.warning(
                f"Failed to apply where clause '{where}': {exc}, returning unfiltered results"
            )

    # Normalize quote data
    for quote in quotes_data:
        # Ensure LineItems is always a list
        if "LineItems" not in quote or quote["LineItems"] is None:
            quote["LineItems"] = []

        # Ensure Contact is always a dict
        if "Contact" not in quote or quote["Contact"] is None:
            quote["Contact"] = {}

    # Apply pagination (page size 100 aligns with Xero defaults)
    page_size = 100
    total_count = len(quotes_data)
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0

    current_page = page if page and page > 0 else 1
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    quotes_data = quotes_data[start_idx:end_idx]
    has_next = current_page < total_pages

    response: dict[str, Any] = {"Quotes": quotes_data}
    response = self._add_metadata(response, "xero-mock", "offline")
    response["meta"].update(
        {
            "page": current_page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_next": has_next,
        }
    )
    return response
