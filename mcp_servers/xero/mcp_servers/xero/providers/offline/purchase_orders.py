"""Purchase orders resource implementation for offline provider."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import PurchaseOrder
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.utils import apply_where_filter, validate_where_clause


async def get_purchase_orders(
    self,
    ids: list[str] | None = None,
    where: str | None = None,
    page: int | None = None,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    """Get purchase orders with optional filtering and pagination.

    Args:
        self: Provider instance
        ids: Optional list of purchase order IDs to filter by
        where: Optional OData filter expression
        page: Optional page number (1-indexed)
        statuses: Optional list of statuses to filter by

    Returns:
        Dictionary containing purchase orders array and metadata
    """
    async with async_session() as session:
        result = await session.execute(select(PurchaseOrder))
        purchase_orders = result.scalars().all()

    purchase_orders_data = [po.to_dict() for po in purchase_orders]

    # Filter by IDs if provided
    if ids:
        purchase_orders_data = [
            po for po in purchase_orders_data if po.get("PurchaseOrderID") in ids
        ]

    # Filter by statuses if provided
    if statuses:
        purchase_orders_data = [po for po in purchase_orders_data if po.get("Status") in statuses]

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
            purchase_orders_data = apply_where_filter(purchase_orders_data, where)
        except ValueError as exc:
            logger.warning(
                f"Failed to apply where clause '{where}': {exc}, returning unfiltered results"
            )

    # Normalize purchase order data
    for po in purchase_orders_data:
        # Ensure LineItems is always a list
        if "LineItems" not in po or po["LineItems"] is None:
            po["LineItems"] = []

        # Ensure Contact is always a dict
        if "Contact" not in po or po["Contact"] is None:
            po["Contact"] = {}

    # Apply pagination (page size 100 aligns with Xero defaults)
    page_size = 100
    total_count = len(purchase_orders_data)
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0

    current_page = page if page and page > 0 else 1
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    purchase_orders_data = purchase_orders_data[start_idx:end_idx]
    has_next = current_page < total_pages

    response: dict[str, Any] = {"PurchaseOrders": purchase_orders_data}
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
