"""Budgets resource implementation for offline provider."""

from typing import Any

from sqlalchemy import select

from mcp_servers.xero.db.models import Budget
from mcp_servers.xero.db.session import async_session


async def get_budgets(self) -> dict[str, Any]:
    """Get budget entities with tracking categories from database."""
    async with async_session() as session:
        result = await session.execute(select(Budget))
        budgets = result.scalars().all()

        # Convert to dict format
        budgets_data = [budget.to_dict() for budget in budgets]

    response = {"Budgets": budgets_data}
    return self._add_metadata(response, "xero-mock", "offline")
