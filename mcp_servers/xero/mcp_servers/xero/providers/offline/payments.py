"""Payments resource implementation for offline provider."""

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import Payment
from mcp_servers.xero.db.session import async_session


async def get_payments(self, where: str | None = None, page: int | None = None) -> dict[str, Any]:
    """Get payments from database."""
    async with async_session() as session:
        result = await session.execute(select(Payment))
        payments = result.scalars().all()

        # Convert to dict format
        payments_data = [payment.to_dict() for payment in payments]

    response = {"Payments": payments_data}
    return self._add_metadata(response, "xero-mock", "offline")
