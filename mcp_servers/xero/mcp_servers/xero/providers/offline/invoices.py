"""Invoices resource implementation for offline provider."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import CreditNote, Invoice
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.utils import apply_where_filter, validate_where_clause


async def get_invoices(
    self,
    ids: list[str] | None = None,
    statuses: list[str] | None = None,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Get invoices from database with filtering and totals validation."""
    async with async_session() as session:
        # Build query
        query = select(Invoice)

        # Filter by IDs if provided
        if ids:
            query = query.where(Invoice.invoice_id.in_(ids))

        # Filter by statuses if provided
        if statuses:
            query = query.where(Invoice.status.in_(statuses))

        result = await session.execute(query)
        invoices = result.scalars().all()

        # Get credit notes to calculate AmountCredited dynamically
        cn_result = await session.execute(select(CreditNote))
        credit_notes = cn_result.scalars().all()

        # Convert to dict format
        invoices_data = [invoice.to_dict() for invoice in invoices]

        # Build mapping of invoice_id -> credited amount from credit note allocations
        # This ensures AmountCredited is always accurate based on actual allocations
        # Process inside session to avoid detached instance issues
        credited_by_invoice: dict[str, float] = {}
        for cn in credit_notes:
            # Skip voided or deleted credit notes - their allocations should not count
            cn_status: str | None = cn.status  # type: ignore[assignment]
            if cn_status and cn_status.upper() in ("VOIDED", "DELETED"):
                continue
            alloc_str: str | None = cn.allocations  # type: ignore[assignment]
            if not alloc_str:
                continue
            try:
                allocations = json.loads(alloc_str)
                if not isinstance(allocations, list):
                    continue
            except (json.JSONDecodeError, TypeError):
                continue
            for alloc in allocations:
                if not isinstance(alloc, dict):
                    continue
                invoice_id = alloc.get("InvoiceID") or alloc.get("invoice_id")
                try:
                    amount = float(alloc.get("Amount", 0) or 0)
                except (ValueError, TypeError):
                    amount = 0.0
                if invoice_id and amount:
                    credited_by_invoice[invoice_id] = (
                        credited_by_invoice.get(invoice_id, 0.0) + amount
                    )

    # Update AmountCredited and recalculate AmountDue for each invoice
    # Also reset AmountCredited for invoices without active allocations (e.g., if credit notes were voided)
    for invoice in invoices_data:
        invoice_id = invoice.get("InvoiceID")
        if invoice_id:
            credited = credited_by_invoice.get(invoice_id, 0.0)
            invoice["AmountCredited"] = round(credited, 2)
            # Recalculate AmountDue: Total - AmountPaid - AmountCredited
            try:
                total = float(invoice.get("Total", 0) or 0)
                paid = float(invoice.get("AmountPaid", 0) or 0)
            except (ValueError, TypeError):
                total = 0.0
                paid = 0.0
            invoice["AmountDue"] = round(total - paid - credited, 2)

    # Validate and apply where clause filter
    if where:
        validate_where_clause(where)
        invoices_data = apply_where_filter(invoices_data, where)

    # Validate totals consistency and log warnings
    for invoice in invoices_data:
        line_items = invoice.get("LineItems", [])
        if line_items:
            # Calculate sum of line amounts using Decimal for precision
            line_total = sum(Decimal(str(item.get("LineAmount", 0))) for item in line_items)
            invoice_total = Decimal(str(invoice.get("Total", 0)))

            # Check consistency within ±0.01 tolerance
            if abs(line_total - invoice_total) > Decimal("0.01"):
                logger.warning(
                    f"Invoice {invoice.get('InvoiceNumber', 'Unknown')} totals inconsistency: "
                    f"LineItems sum={line_total:.2f}, Total={invoice_total:.2f}, "
                    f"difference={abs(line_total - invoice_total):.2f}"
                )

    # Apply pagination (page size 100 aligns with Xero defaults)
    page_size = 100
    if page is not None and page > 0:
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        invoices_data = invoices_data[start_idx:end_idx]

    response = {"Invoices": invoices_data}
    return self._add_metadata(response, "xero-mock", "offline")
