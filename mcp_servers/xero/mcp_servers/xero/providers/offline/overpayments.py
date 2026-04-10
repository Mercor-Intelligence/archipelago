"""Overpayments resource implementation for offline provider."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import Overpayment
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.utils import apply_where_filter, validate_where_clause


async def get_overpayments(
    self,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Get overpayment records with optional filtering and pagination."""
    async with async_session() as session:
        result = await session.execute(select(Overpayment))
        overpayments = result.scalars().all()

    overpayments_data = [op.to_dict() for op in overpayments]

    if where:
        # Try validation first, but still attempt filtering even if validation fails
        # (apply_where_filter may handle formats that validation rejects, e.g., Guid(...))
        try:
            validate_where_clause(where)
        except ValueError as exc:
            logger.warning(
                f"Where clause validation failed for '{where}': {exc}, attempting filter anyway"
            )

        # Attempt to apply the filter regardless of validation result
        try:
            overpayments_data = apply_where_filter(overpayments_data, where)
        except ValueError as exc:
            # If filtering also fails, fall back to unfiltered results
            logger.warning(
                f"Failed to apply where clause '{where}': {exc}, returning unfiltered results"
            )
            # Do not hide data; fall back to unfiltered results
            pass

    # Normalize allocations, dates, and remaining credit fields
    for op in overpayments_data:
        allocations = op.get("Allocations") or []
        normalized_allocations: list[dict[str, Any]] = []
        for allocation in allocations:
            if allocation is None:
                continue
            alloc_copy = dict(allocation)
            if "DateString" not in alloc_copy and alloc_copy.get("Date"):
                alloc_copy["DateString"] = alloc_copy["Date"]
            if "Invoice" not in alloc_copy:
                alloc_copy["Invoice"] = None
            normalized_allocations.append(alloc_copy)

        op["Allocations"] = normalized_allocations

        if "DateString" not in op and op.get("Date"):
            op["DateString"] = op["Date"]

        if "LineItems" not in op or op["LineItems"] is None:
            op["LineItems"] = []

        total = op.get("Total")
        if total is not None:
            allocated_sum = sum(float(a.get("Amount", 0) or 0) for a in op["Allocations"])
            remaining = float(total) - allocated_sum
            # Keep two-decimal precision like Xero responses
            op["RemainingCredit"] = round(remaining, 2)

        # Ensure allocations list exists even when empty
        if "Allocations" not in op or op["Allocations"] is None:
            op["Allocations"] = []

    # Apply pagination (page size 100 aligns with Xero defaults)
    page_size = 100
    total_count = len(overpayments_data)
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0

    current_page = page if page and page > 0 else 1
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    overpayments_data = overpayments_data[start_idx:end_idx]
    has_next = current_page < total_pages

    response: dict[str, Any] = {"Overpayments": overpayments_data}
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
