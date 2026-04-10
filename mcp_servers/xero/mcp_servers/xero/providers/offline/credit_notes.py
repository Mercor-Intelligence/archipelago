"""Credit notes resource implementation for offline provider."""

from __future__ import annotations

import json
from typing import Any, cast

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import CreditNote, Invoice
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.utils import apply_where_filter, validate_where_clause


def _detect_implicit_allocations(
    credit_note: dict[str, Any],
    invoices_with_credit: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect implicit allocations from invoices with AmountCredited > 0.

    When a credit note's Allocations array is empty but invoices from the same
    contact have AmountCredited > 0, we can infer the allocation.

    Args:
        credit_note: Credit note data dictionary
        invoices_with_credit: List of invoices that have AmountCredited > 0

    Returns:
        List of detected allocation dictionaries
    """
    allocations = []

    cn_contact = credit_note.get("Contact", {})
    cn_contact_id = cn_contact.get("ContactID") if isinstance(cn_contact, dict) else None
    cn_type = credit_note.get("Type", "")
    cn_total = float(credit_note.get("Total", 0) or 0)

    if not cn_contact_id or cn_total == 0:
        return allocations

    # Match credit notes to invoices by contact and type
    # ACCRECCREDIT -> ACCREC invoices, ACCPAYCREDIT -> ACCPAY invoices
    expected_invoice_type = "ACCREC" if "ACCREC" in cn_type else "ACCPAY"

    remaining_to_allocate = cn_total
    for invoice in invoices_with_credit:
        if remaining_to_allocate <= 0:
            break

        inv_contact = invoice.get("Contact", {})
        inv_contact_id = inv_contact.get("ContactID") if isinstance(inv_contact, dict) else None
        inv_type = invoice.get("Type", "")
        amount_credited = float(invoice.get("AmountCredited", 0) or 0)

        # Match by contact and invoice type
        if inv_contact_id != cn_contact_id:
            continue
        if inv_type != expected_invoice_type:
            continue
        if amount_credited <= 0:
            continue

        # Allocate up to the remaining credit amount
        allocation_amount = min(amount_credited, remaining_to_allocate)
        allocations.append(
            {
                "Amount": allocation_amount,
                "Invoice": {
                    "InvoiceID": invoice.get("InvoiceID"),
                    "InvoiceNumber": invoice.get("InvoiceNumber"),
                },
            }
        )
        remaining_to_allocate -= allocation_amount

    return allocations


async def get_credit_notes(
    self,
    ids: list[str] | None = None,
    where: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Get credit notes with optional filtering and pagination.

    Args:
        self: Provider instance
        ids: Optional list of credit note IDs to filter by
        where: Optional OData filter expression
        page: Optional page number (1-indexed)

    Returns:
        Dictionary containing credit notes array and metadata
    """
    async with async_session() as session:
        result = await session.execute(select(CreditNote))
        credit_notes = result.scalars().all()

        # Also query invoices to detect implicit allocations
        invoice_result = await session.execute(select(Invoice))
        invoices = invoice_result.scalars().all()

    credit_notes_data = [cn.to_dict() for cn in credit_notes]

    # Build list of invoices with AmountCredited > 0 for allocation detection
    invoices_with_credit = []
    for inv in invoices:
        # Cast SQLAlchemy Column types to runtime values
        raw_amount_credited = cast("float | None", inv.amount_credited)
        amount_credited = float(raw_amount_credited) if raw_amount_credited else 0.0
        if amount_credited > 0:
            contact_data: dict[str, Any] = {}
            raw_contact = cast("str | None", inv.contact)
            if raw_contact:
                try:
                    contact_data = json.loads(raw_contact)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            invoices_with_credit.append(
                {
                    "InvoiceID": cast("str | None", inv.invoice_id),
                    "InvoiceNumber": cast("str | None", inv.invoice_number),
                    "Type": cast("str | None", inv.type),
                    "Contact": contact_data,
                    "AmountCredited": amount_credited,
                }
            )

    # Filter by IDs if provided
    if ids:
        credit_notes_data = [cn for cn in credit_notes_data if cn.get("CreditNoteID") in ids]

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
            credit_notes_data = apply_where_filter(credit_notes_data, where)
        except ValueError as exc:
            logger.warning(
                f"Failed to apply where clause '{where}': {exc}, returning unfiltered results"
            )

    # Normalize credit note data
    for cn in credit_notes_data:
        # Ensure LineItems is always a list
        if "LineItems" not in cn or cn["LineItems"] is None:
            cn["LineItems"] = []

        # Ensure Contact is always a dict
        if "Contact" not in cn or cn["Contact"] is None:
            cn["Contact"] = {}

        # Ensure Allocations is always a list
        if "Allocations" not in cn or cn["Allocations"] is None:
            cn["Allocations"] = []

        # If Allocations is empty, try to detect implicit allocations from invoices
        if not cn["Allocations"] and invoices_with_credit:
            detected_allocations = _detect_implicit_allocations(cn, invoices_with_credit)
            if detected_allocations:
                cn["Allocations"] = detected_allocations
                logger.debug(
                    f"Detected {len(detected_allocations)} implicit allocation(s) "
                    f"for credit note {cn.get('CreditNoteID')}"
                )

        # Calculate RemainingCredit dynamically from Total minus allocated amounts
        total = cn.get("Total")
        if total is not None:
            allocated_sum = sum(float(a.get("Amount", 0) or 0) for a in cn["Allocations"])
            remaining = float(total) - allocated_sum
            # Keep two-decimal precision like Xero responses
            cn["RemainingCredit"] = round(remaining, 2)

    # Apply pagination (page size 100 aligns with Xero defaults)
    page_size = 100
    total_count = len(credit_notes_data)
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0

    current_page = page if page and page > 0 else 1
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    credit_notes_data = credit_notes_data[start_idx:end_idx]
    has_next = current_page < total_pages

    response: dict[str, Any] = {"CreditNotes": credit_notes_data}
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
