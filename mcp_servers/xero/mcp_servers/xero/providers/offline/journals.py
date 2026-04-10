"""Journals resource implementation for offline provider."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import Journal
from mcp_servers.xero.db.session import async_session

PAYMENT_SOURCE_TYPES = {"CASHREC", "CASHPAID", "ACCRECPAYMENT", "ACCPAYPAYMENT"}


async def get_journals(
    self,
    offset: int | None = None,
    payments_only: bool | None = None,
) -> dict[str, Any]:
    """Get manual journal entries with pagination and payments filtering."""
    async with async_session() as session:
        stmt = select(Journal).order_by(Journal.journal_number)
        result = await session.execute(stmt)
        journals = result.scalars().all()

    journals_data = [journal.to_dict() for journal in journals]

    if payments_only:
        journals_data = _filter_payment_journals(journals_data)

    offset_value = offset if offset is not None else 0
    if offset_value:
        journals_data = journals_data[offset_value:]

    response = {"Journals": journals_data}
    return self._add_metadata(response, "xero-mock", "offline")


def _filter_payment_journals(journals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only journals whose SourceType represents a payment."""
    return [journal for journal in journals if _is_payment_source(journal.get("SourceType"))]


def _is_payment_source(source_type: str | None) -> bool:
    """Identify payment-related source types."""
    if not source_type:
        return False
    return source_type.upper() in PAYMENT_SOURCE_TYPES
