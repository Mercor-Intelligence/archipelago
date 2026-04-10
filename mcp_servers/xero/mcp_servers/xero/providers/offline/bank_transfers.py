"""Bank transfers resource implementation for offline provider."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import BankTransfer
from mcp_servers.xero.db.session import async_session
from mcp_servers.xero.utils import apply_where_filter, validate_where_clause


async def get_bank_transfers(
    self,
    where: str | None = None,
) -> dict[str, Any]:
    """Get inter-account transfers with optional filtering.

    Args:
        where: Filter expression (e.g., 'BankTransferID=="uuid"')

    Returns:
        Dictionary with BankTransfers array and metadata

    Raises:
        ValueError: If where clause is malformed
    """
    async with async_session() as session:
        stmt = select(BankTransfer)
        result = await session.execute(stmt)
        transfers = result.scalars().all()

    transfers_data = [transfer.to_dict() for transfer in transfers]

    # Validate and apply where clause filter
    if where:
        validate_where_clause(where)
        transfers_data = apply_where_filter(transfers_data, where)

    response = {"BankTransfers": transfers_data}
    return self._add_metadata(response, "xero-mock", "offline")
