"""Workspace repository for session-scoped database operations."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import (
    ApplicationSnapshot,
    AuditLog,
    DocumentRecord,
    ForeignPriorityRecord,
    SavedQuery,
    Workspace,
)


class WorkspaceRepository:
    """Session-scoped database operations for workspaces."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_workspace(
        self,
        id: str,
        name: str,
        description: str | None,
        metadata: str | None,
    ) -> Workspace:
        """Create new workspace in session database."""
        workspace = Workspace(
            id=id,
            name=name,
            description=description,
            metadata_json=metadata,
        )
        self.session.add(workspace)
        await self.session.flush()
        await self.session.refresh(workspace)
        return workspace

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Get workspace by ID from session database."""
        result = await self.session.execute(select(Workspace).where(Workspace.id == workspace_id))
        return result.scalar_one_or_none()

    async def get_workspace_by_name(self, name: str) -> Workspace | None:
        """Get workspace by name from session database."""
        result = await self.session.execute(select(Workspace).where(Workspace.name == name))
        return result.scalar_one_or_none()

    async def list_workspaces(
        self,
        offset: int,
        limit: int,
    ) -> list[Workspace]:
        """List all workspaces in session with pagination."""
        result = await self.session.execute(
            select(Workspace).order_by(Workspace.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars().all())

    async def get_workspace_stats(self, workspace_id: str) -> dict[str, int]:
        """Get workspace statistics (saved queries, snapshots, etc.)."""
        stats = await self.get_batch_workspace_stats([workspace_id])
        return stats.get(
            workspace_id,
            {
                "saved_queries": 0,
                "snapshots": 0,
                "documents_retrieved": 0,
                "foreign_priority_records": 0,
            },
        )

    async def get_batch_workspace_stats(
        self, workspace_ids: list[str]
    ) -> dict[str, dict[str, int]]:
        """Get workspace statistics for multiple workspaces in batch (avoids N+1)."""
        if not workspace_ids:
            return {}

        # Initialize results with zeros
        results: dict[str, dict[str, int]] = {
            ws_id: {
                "saved_queries": 0,
                "snapshots": 0,
                "documents_retrieved": 0,
                "foreign_priority_records": 0,
            }
            for ws_id in workspace_ids
        }

        # Batch count saved queries
        saved_queries_result = await self.session.execute(
            select(SavedQuery.workspace_id, func.count(SavedQuery.id))
            .where(SavedQuery.workspace_id.in_(workspace_ids))
            .group_by(SavedQuery.workspace_id)
        )
        for ws_id, count in saved_queries_result:
            results[ws_id]["saved_queries"] = count

        # Batch count snapshots
        snapshots_result = await self.session.execute(
            select(ApplicationSnapshot.workspace_id, func.count(ApplicationSnapshot.id))
            .where(ApplicationSnapshot.workspace_id.in_(workspace_ids))
            .group_by(ApplicationSnapshot.workspace_id)
        )
        for ws_id, count in snapshots_result:
            results[ws_id]["snapshots"] = count

        # Batch count documents
        documents_result = await self.session.execute(
            select(DocumentRecord.workspace_id, func.count(DocumentRecord.id))
            .where(DocumentRecord.workspace_id.in_(workspace_ids))
            .group_by(DocumentRecord.workspace_id)
        )
        for ws_id, count in documents_result:
            results[ws_id]["documents_retrieved"] = count

        # Batch count foreign priority records
        foreign_priority_result = await self.session.execute(
            select(ForeignPriorityRecord.workspace_id, func.count(ForeignPriorityRecord.id))
            .where(ForeignPriorityRecord.workspace_id.in_(workspace_ids))
            .group_by(ForeignPriorityRecord.workspace_id)
        )
        for ws_id, count in foreign_priority_result:
            results[ws_id]["foreign_priority_records"] = count

        return results

    async def get_recent_activity(
        self,
        workspace_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Get recent audit log entries for workspace."""
        result = await self.session.execute(
            select(AuditLog)
            .where(AuditLog.workspace_id == workspace_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        audit_logs = result.scalars().all()

        # Convert audit logs to activity format
        activities = []
        for log in audit_logs:
            details = json.loads(log.details) if log.details else {}
            activities.append(
                {
                    "action": log.action,
                    "applicationNumber": details.get("applicationNumberText"),
                    "timestamp": log.created_at,
                }
            )

        return activities


__all__ = ["WorkspaceRepository"]
