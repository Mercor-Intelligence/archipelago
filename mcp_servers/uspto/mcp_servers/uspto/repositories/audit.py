"""Audit log repository for session-scoped database operations."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import AuditLog


class AuditRepository:
    """Session-scoped database operations for audit logs."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_audit_entries(
        self,
        workspace_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[AuditLog]:
        """
        List audit entries for a workspace with optional date filtering.

        Returns entries ordered by created_at DESC (newest first).
        """
        query = select(AuditLog).where(AuditLog.workspace_id == workspace_id)

        if start_date:
            query = query.where(AuditLog.created_at >= start_date)

        if end_date:
            query = query.where(AuditLog.created_at <= end_date)

        query = query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_audit_entries(
        self,
        workspace_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """Count total audit entries for a workspace with optional date filtering."""
        query = select(func.count(AuditLog.id)).where(AuditLog.workspace_id == workspace_id)

        if start_date:
            query = query.where(AuditLog.created_at >= start_date)

        if end_date:
            query = query.where(AuditLog.created_at <= end_date)

        result = await self.session.execute(query)
        return result.scalar_one()

    async def workspace_exists(self, workspace_id: str) -> bool:
        """Check if a workspace has any audit entries (for validation)."""
        from mcp_servers.uspto.db.models import Workspace

        result = await self.session.execute(
            select(func.count(Workspace.id)).where(Workspace.id == workspace_id)
        )
        return result.scalar_one() > 0


__all__ = ["AuditRepository"]
