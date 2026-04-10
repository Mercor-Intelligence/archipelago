"""Bundle export repository for session-scoped database operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import (
    ApplicationSnapshot,
    AuditLog,
    DocumentRecord,
    ForeignPriorityRecord,
    SavedQuery,
)


class BundlesRepository:
    """Session-scoped database operations for bundle exports."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_queries_by_ids(self, workspace_id: str, query_ids: list[str]) -> list[SavedQuery]:
        """Get saved queries by IDs for a workspace."""
        result = await self.session.execute(
            select(SavedQuery).where(
                SavedQuery.workspace_id == workspace_id,
                SavedQuery.id.in_(query_ids),
            )
        )
        return list(result.scalars().all())

    async def get_all_queries(self, workspace_id: str) -> list[SavedQuery]:
        """Get all saved queries for a workspace."""
        result = await self.session.execute(
            select(SavedQuery).where(SavedQuery.workspace_id == workspace_id)
        )
        return list(result.scalars().all())

    async def get_snapshots_by_app_numbers(
        self, workspace_id: str, application_numbers: list[str]
    ) -> list[ApplicationSnapshot]:
        """Get all snapshots (all versions) for specified application numbers."""
        result = await self.session.execute(
            select(ApplicationSnapshot)
            .where(
                ApplicationSnapshot.workspace_id == workspace_id,
                ApplicationSnapshot.application_number_text.in_(application_numbers),
            )
            .order_by(
                ApplicationSnapshot.application_number_text,
                ApplicationSnapshot.version.desc(),
            )
        )
        return list(result.scalars().all())

    async def get_all_snapshots(self, workspace_id: str) -> list[ApplicationSnapshot]:
        """Get all snapshots (all versions) for a workspace."""
        result = await self.session.execute(
            select(ApplicationSnapshot)
            .where(ApplicationSnapshot.workspace_id == workspace_id)
            .order_by(
                ApplicationSnapshot.application_number_text,
                ApplicationSnapshot.version.desc(),
            )
        )
        return list(result.scalars().all())

    async def get_documents_for_application(
        self, workspace_id: str, application_number: str
    ) -> list[DocumentRecord]:
        """Get all documents for an application number."""
        result = await self.session.execute(
            select(DocumentRecord).where(
                DocumentRecord.workspace_id == workspace_id,
                DocumentRecord.application_number_text == application_number,
            )
        )
        return list(result.scalars().all())

    async def get_documents_for_applications(
        self, workspace_id: str, application_numbers: list[str]
    ) -> list[DocumentRecord]:
        """Get all documents for multiple application numbers."""
        if not application_numbers:
            return []
        result = await self.session.execute(
            select(DocumentRecord).where(
                DocumentRecord.workspace_id == workspace_id,
                DocumentRecord.application_number_text.in_(application_numbers),
            )
        )
        return list(result.scalars().all())

    async def get_foreign_priority_for_application(
        self, workspace_id: str, application_number: str
    ) -> list[ForeignPriorityRecord]:
        """Get all foreign priority records for an application number."""
        result = await self.session.execute(
            select(ForeignPriorityRecord).where(
                ForeignPriorityRecord.workspace_id == workspace_id,
                ForeignPriorityRecord.application_number_text == application_number,
            )
        )
        return list(result.scalars().all())

    async def get_foreign_priority_for_applications(
        self, workspace_id: str, application_numbers: list[str]
    ) -> list[ForeignPriorityRecord]:
        """Get all foreign priority records for multiple application numbers."""
        if not application_numbers:
            return []
        result = await self.session.execute(
            select(ForeignPriorityRecord).where(
                ForeignPriorityRecord.workspace_id == workspace_id,
                ForeignPriorityRecord.application_number_text.in_(application_numbers),
            )
        )
        return list(result.scalars().all())

    async def get_audit_log_for_workspace(self, workspace_id: str) -> list[AuditLog]:
        """Get all audit log entries for a workspace."""
        result = await self.session.execute(
            select(AuditLog)
            .where(AuditLog.workspace_id == workspace_id)
            .order_by(AuditLog.created_at.desc())
        )
        return list(result.scalars().all())


__all__ = ["BundlesRepository"]
