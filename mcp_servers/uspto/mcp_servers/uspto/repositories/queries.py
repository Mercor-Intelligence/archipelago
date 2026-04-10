"""Saved queries repository for session-scoped database operations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import SavedQuery


class QueriesRepository:
    """Session-scoped database operations for saved queries."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_saved_query(
        self,
        id: str,
        workspace_id: str,
        name: str,
        query_text: str,
        filters: str | None,
        pinned_results: str | None,
        notes: str | None,
    ) -> SavedQuery:
        """Create new saved query in session database."""
        saved_query = SavedQuery(
            id=id,
            workspace_id=workspace_id,
            name=name,
            query_text=query_text,
            filters=filters,
            pinned_results=pinned_results,
            notes=notes,
        )
        self.session.add(saved_query)
        await self.session.flush()
        await self.session.refresh(saved_query)
        return saved_query

    async def get_saved_query(self, query_id: str) -> SavedQuery | None:
        """Get saved query by ID from session database."""
        result = await self.session.execute(select(SavedQuery).where(SavedQuery.id == query_id))
        return result.scalar_one_or_none()

    async def get_query_by_name(self, workspace_id: str, name: str) -> SavedQuery | None:
        """Get saved query by name within a workspace."""
        result = await self.session.execute(
            select(SavedQuery).where(
                SavedQuery.workspace_id == workspace_id,
                SavedQuery.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def update_query_execution(
        self,
        query_id: str,
        last_run_at: datetime,
    ) -> None:
        """Update execution metadata for a saved query atomically.

        Uses SQL increment (run_count = run_count + 1) to avoid race conditions.
        """
        # Convert to UTC if timezone-aware, then format with Z suffix
        if last_run_at.tzinfo is not None:
            utc_time = last_run_at.astimezone(UTC)
            timestamp = utc_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            # Naive datetime assumed to be UTC
            timestamp = f"{last_run_at.isoformat()}Z"

        await self.session.execute(
            update(SavedQuery)
            .where(SavedQuery.id == query_id)
            .values(
                last_run_at=timestamp,
                run_count=SavedQuery.run_count + 1,  # Atomic increment
            )
        )
        await self.session.flush()


__all__ = ["QueriesRepository"]
