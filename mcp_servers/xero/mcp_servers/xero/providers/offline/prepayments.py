"""Prepayments resource implementation for offline provider."""

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import Prepayment
from mcp_servers.xero.db.session import async_session


async def get_prepayments(
    self,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Get prepayment records from database.

    Args:
        where: Optional filter expression (not yet implemented for offline)
        page: Optional page number for pagination (1-indexed)

    Returns:
        Dictionary containing prepayments array and metadata
    """
    async with async_session() as session:
        result = await session.execute(select(Prepayment))
        prepayments = result.scalars().all()

        # Convert to dict format
        prepayments_data = [prepayment.to_dict() for prepayment in prepayments]

    # Ensure Allocations is always an array (even if empty)
    for prepayment in prepayments_data:
        if "Allocations" not in prepayment or prepayment["Allocations"] is None:
            prepayment["Allocations"] = []

    # Apply pagination (page size 100 aligns with Xero defaults)
    page_size = 100
    total_count = len(prepayments_data)
    current_page = page if page is not None and page > 0 else 1
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    prepayments_data = prepayments_data[start_idx:end_idx]
    has_next = start_idx + len(prepayments_data) < total_count

    response = {"Prepayments": prepayments_data}
    response = self._add_metadata(response, "xero-mock", "offline")
    response["meta"]["page"] = current_page
    response["meta"]["page_size"] = page_size
    response["meta"]["has_next"] = has_next
    return response
