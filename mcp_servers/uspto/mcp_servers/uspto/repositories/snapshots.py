"""Application snapshots repository for session-scoped database operations."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import ApplicationSnapshot


class SnapshotRepository:
    """Session-scoped database operations for application snapshots."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_snapshot(
        self,
        id: str,
        workspace_id: str,
        application_number_text: str,
        version: int,
        invention_title: str | None,
        filing_date: str | None,
        publication_date: str | None,
        publication_number: str | None,
        patent_number: str | None,
        patent_issue_date: str | None,
        application_status_code: str | None,
        application_status_description: str | None,
        status_normalized_at: str | None,
        status_code_version: str | None,
        first_inventor_name: str | None,
        first_applicant_name: str | None,
        assignee_entity_name: str | None,
        examiner_name: str | None,
        group_art_unit_number: str | None,
        uspc_class: str | None,
        uspc_subclass: str | None,
        cpc_classifications: str | None,
        entity_status: str | None,
        application_type: str | None,
        confidential: bool | None,
        raw_uspto_response: str,
        retrieved_at: str,
        priority_claims_json: str | None = None,
    ) -> ApplicationSnapshot:
        """Create new application snapshot in session database."""
        snapshot = ApplicationSnapshot(
            id=id,
            workspace_id=workspace_id,
            application_number_text=application_number_text,
            version=version,
            invention_title=invention_title,
            filing_date=filing_date,
            publication_date=publication_date,
            publication_number=publication_number,
            patent_number=patent_number,
            patent_issue_date=patent_issue_date,
            application_status_code=application_status_code,
            application_status_description=application_status_description,
            status_normalized_at=status_normalized_at,
            status_code_version=status_code_version,
            first_inventor_name=first_inventor_name,
            first_applicant_name=first_applicant_name,
            assignee_entity_name=assignee_entity_name,
            examiner_name=examiner_name,
            group_art_unit_number=group_art_unit_number,
            uspc_class=uspc_class,
            uspc_subclass=uspc_subclass,
            cpc_classifications=cpc_classifications,
            entity_status=entity_status,
            application_type=application_type,
            confidential=confidential,
            raw_uspto_response=raw_uspto_response,
            priority_claims_json=priority_claims_json,
            retrieved_at=retrieved_at,
        )
        self.session.add(snapshot)
        await self.session.flush()
        await self.session.refresh(snapshot)
        return snapshot

    async def get_snapshot_by_app_and_version(
        self,
        workspace_id: str,
        application_number_text: str,
        version: int | None = None,
    ) -> ApplicationSnapshot | None:
        """
        Retrieve snapshot by application number and version.

        If version is None, retrieves the latest version (MAX version).
        """
        if version is None:
            # Subquery to get max version for this application
            max_version_subq = (
                select(func.max(ApplicationSnapshot.version))
                .where(
                    ApplicationSnapshot.workspace_id == workspace_id,
                    ApplicationSnapshot.application_number_text == application_number_text,
                )
                .scalar_subquery()
            )

            # Main query to get snapshot with max version
            result = await self.session.execute(
                select(ApplicationSnapshot).where(
                    ApplicationSnapshot.workspace_id == workspace_id,
                    ApplicationSnapshot.application_number_text == application_number_text,
                    ApplicationSnapshot.version == max_version_subq,
                )
            )
        else:
            # Get specific version
            result = await self.session.execute(
                select(ApplicationSnapshot).where(
                    ApplicationSnapshot.workspace_id == workspace_id,
                    ApplicationSnapshot.application_number_text == application_number_text,
                    ApplicationSnapshot.version == version,
                )
            )

        return result.scalar_one_or_none()

    async def get_next_version_number(self, workspace_id: str, application_number_text: str) -> int:
        """
        Determine next version number for an application.

        Returns 1 for new applications, or MAX(version) + 1 for existing ones.
        """
        result = await self.session.execute(
            select(func.max(ApplicationSnapshot.version)).where(
                ApplicationSnapshot.workspace_id == workspace_id,
                ApplicationSnapshot.application_number_text == application_number_text,
            )
        )
        max_version = result.scalar_one_or_none()
        return (max_version + 1) if max_version else 1

    async def list_snapshots(
        self, workspace_id: str, offset: int, limit: int
    ) -> list[ApplicationSnapshot]:
        """List snapshots in workspace with pagination, ordered by created_at DESC."""
        result = await self.session.execute(
            select(ApplicationSnapshot)
            .where(ApplicationSnapshot.workspace_id == workspace_id)
            .order_by(ApplicationSnapshot.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_snapshots(self, workspace_id: str) -> int:
        """Count total snapshots in workspace for pagination."""
        result = await self.session.execute(
            select(func.count(ApplicationSnapshot.id)).where(
                ApplicationSnapshot.workspace_id == workspace_id
            )
        )
        return result.scalar_one()

    async def get_latest_snapshots_by_app_numbers(
        self, workspace_id: str, application_numbers: list[str]
    ) -> list[ApplicationSnapshot]:
        """Get latest snapshot per application number for a workspace."""
        if not application_numbers:
            return []

        latest_version = (
            select(
                ApplicationSnapshot.application_number_text.label("app_number"),
                func.max(ApplicationSnapshot.version).label("max_version"),
            )
            .where(
                ApplicationSnapshot.workspace_id == workspace_id,
                ApplicationSnapshot.application_number_text.in_(application_numbers),
            )
            .group_by(ApplicationSnapshot.application_number_text)
            .subquery()
        )

        result = await self.session.execute(
            select(ApplicationSnapshot)
            .join(
                latest_version,
                (ApplicationSnapshot.application_number_text == latest_version.c.app_number)
                & (ApplicationSnapshot.version == latest_version.c.max_version)
                & (ApplicationSnapshot.workspace_id == workspace_id),
            )
            .order_by(ApplicationSnapshot.application_number_text)
        )
        return list(result.scalars().all())


__all__ = ["SnapshotRepository"]
