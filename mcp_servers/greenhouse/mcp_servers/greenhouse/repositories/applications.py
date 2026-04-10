"""Application repository for Greenhouse MCP Server.

Handles data access for Application entities with Harvest API response formatting.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from db.models import Application, JobStage, RejectionReason
from repositories.base import BaseRepository
from repositories.exceptions import ConflictError, NotFoundError, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload


class ApplicationRepository(BaseRepository[Application]):
    """Repository for Application entity CRUD operations.

    Implements standard repository pattern for applications with filtering,
    pagination, and Harvest API response formatting.
    """

    model = Application

    async def get(self, id: int) -> dict | None:
        """Get a single application by ID.

        Args:
            id: Application ID

        Returns:
            Application data as dict if found, None otherwise
        """
        query = (
            select(Application)
            .options(
                selectinload(Application.candidate),
                selectinload(Application.job),
                selectinload(Application.current_stage),
                selectinload(Application.rejection_reason),
                selectinload(Application.source),
                selectinload(Application.answers),
            )
            .where(Application.id == id)
        )
        result = await self.session.execute(query)
        application = result.scalar_one_or_none()
        if application is None:
            return None
        return self._serialize(application)

    async def get_or_raise(self, id: int) -> dict:
        """Get a single application by ID or raise NotFoundError.

        Args:
            id: Application ID

        Returns:
            Application data as dict

        Raises:
            NotFoundError: If application doesn't exist
        """
        application = await self.get(id)
        if application is None:
            raise NotFoundError(f"Application with id {id} does not exist")
        return application

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List applications with optional filters and pagination.

        Args:
            filters: Optional filter criteria:
                - job_id: Filter by job
                - candidate_id: Filter by candidate
                - status: Filter by status (active, rejected, hired)
                - created_before: ISO 8601 timestamp
                - created_after: ISO 8601 timestamp
                - updated_before: ISO 8601 timestamp
                - updated_after: ISO 8601 timestamp
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of application data dicts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = (
            select(Application)
            .options(
                selectinload(Application.candidate),
                selectinload(Application.job),
                selectinload(Application.current_stage),
                selectinload(Application.rejection_reason),
                selectinload(Application.source),
                selectinload(Application.answers),
            )
            .order_by(Application.id)
        )

        if filter_clauses:
            query = query.where(*filter_clauses)

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        applications = result.scalars().unique().all()

        return [self._serialize(app) for app in applications]

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count applications matching filters.

        Args:
            filters: Optional filter criteria (same as list)

        Returns:
            Number of matching applications
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = select(func.count()).select_from(Application)
        if filter_clauses:
            query = query.where(*filter_clauses)

        result = await self.session.scalar(query)
        return result or 0

    async def create(self, data: dict[str, Any]) -> dict:
        """Create a new application.

        Args:
            data: Application data with fields:
                - candidate_id: Candidate ID (required)
                - job_id: Job ID (required)
                - source_id: Source ID
                - credited_to_id: Credited user ID (referrer)
                - attachments: List of attachment data

        Returns:
            Created application data as dict

        Raises:
            ValidationError: If candidate or job doesn't exist
        """
        # Get first stage of the job for initial stage
        stage_query = (
            select(JobStage)
            .where(JobStage.job_id == data["job_id"])
            .order_by(JobStage.priority)
            .limit(1)
        )
        stage_result = await self.session.execute(stage_query)
        first_stage = stage_result.scalar_one_or_none()

        application = Application(
            candidate_id=data["candidate_id"],
            job_id=data["job_id"],
            source_id=data.get("source_id"),
            credited_to_id=data.get("credited_to_id"),
            current_stage_id=first_stage.id if first_stage else None,
            status="active",
        )
        self.session.add(application)
        await self.session.flush()

        return await self.get_or_raise(application.id)

    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing application.

        Args:
            id: Application ID
            data: Updated application data

        Returns:
            Updated application data as dict

        Raises:
            NotFoundError: If application doesn't exist
        """
        application = await self._get_by_id_or_raise(id, "Application")

        if "status" in data:
            application.status = data["status"]
        if "current_stage_id" in data:
            application.current_stage_id = data["current_stage_id"]
        if "rejection_reason_id" in data:
            application.rejection_reason_id = data["rejection_reason_id"]
        if "rejected_at" in data:
            application.rejected_at = data["rejected_at"]

        await self.session.flush()
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """Delete an application by ID.

        Args:
            id: Application ID

        Returns:
            True if deleted, False if not found
        """
        application = await self._get_by_id(id)
        if application is None:
            return False
        await self.session.delete(application)
        await self.session.flush()
        return True

    async def advance_stage(self, id: int) -> dict:
        """Advance application to next pipeline stage.

        Args:
            id: Application ID

        Returns:
            Updated application data as dict

        Raises:
            NotFoundError: If application doesn't exist
            ConflictError: If application is not active or already at final stage
        """
        application = await self._get_by_id_or_raise(id, "Application")

        if application.status != "active":
            msg = f"Cannot advance non-active application (status: {application.status})"
            raise ConflictError(msg)

        # Get next stage
        if application.current_stage_id is None:
            raise ConflictError("Application has no current stage")

        # First, get the current stage's priority (avoid lazy loading)
        current_stage_query = select(JobStage).where(JobStage.id == application.current_stage_id)
        current_stage_result = await self.session.execute(current_stage_query)
        current_stage = current_stage_result.scalar_one_or_none()

        if current_stage is None:
            raise ConflictError("Current stage not found")

        next_stage_query = (
            select(JobStage)
            .where(
                JobStage.job_id == application.job_id,
                JobStage.priority > current_stage.priority,
            )
            .order_by(JobStage.priority)
            .limit(1)
        )
        result = await self.session.execute(next_stage_query)
        next_stage = result.scalar_one_or_none()

        if next_stage is None:
            raise ConflictError("Application is already at the final stage")

        application.current_stage_id = next_stage.id
        await self.session.flush()

        return await self.get_or_raise(id)

    async def move_stage(self, id: int, stage_id: int) -> dict:
        """Move application to a specific pipeline stage.

        Args:
            id: Application ID
            stage_id: Target stage ID

        Returns:
            Updated application data as dict

        Raises:
            NotFoundError: If application or stage doesn't exist
            ValidationError: If stage doesn't belong to the job
        """
        application = await self._get_by_id_or_raise(id, "Application")

        # Verify stage exists and belongs to the job
        stage_query = select(JobStage).where(
            JobStage.id == stage_id,
            JobStage.job_id == application.job_id,
        )
        result = await self.session.execute(stage_query)
        stage = result.scalar_one_or_none()

        if stage is None:
            raise ValidationError(f"Stage {stage_id} does not exist for job {application.job_id}")

        application.current_stage_id = stage_id
        await self.session.flush()

        return await self.get_or_raise(id)

    async def reject(self, id: int, rejection_reason_id: int | None = None) -> dict:
        """Reject an application.

        Args:
            id: Application ID
            rejection_reason_id: Optional rejection reason ID

        Returns:
            Updated application data as dict

        Raises:
            NotFoundError: If application doesn't exist
            ConflictError: If application is not active
        """
        from datetime import datetime

        application = await self._get_by_id_or_raise(id, "Application")

        if application.status != "active":
            msg = f"Cannot reject non-active application (status: {application.status})"
            raise ConflictError(msg)

        application.status = "rejected"
        application.rejected_at = datetime.now(UTC).isoformat()
        if rejection_reason_id:
            application.rejection_reason_id = rejection_reason_id

        await self.session.flush()
        return await self.get_or_raise(id)

    async def hire(self, id: int) -> dict:
        """Hire a candidate (mark application as hired).

        Args:
            id: Application ID

        Returns:
            Updated application data as dict

        Raises:
            NotFoundError: If application doesn't exist
            ConflictError: If application is not active
        """
        application = await self._get_by_id_or_raise(id, "Application")

        if application.status != "active":
            msg = f"Cannot hire non-active application (status: {application.status})"
            raise ConflictError(msg)

        application.status = "hired"
        await self.session.flush()

        return await self.get_or_raise(id)

    async def get_rejection_reasons(self) -> list[dict]:
        """Get all rejection reasons.

        Returns:
            List of rejection reason data dicts
        """
        query = select(RejectionReason).order_by(RejectionReason.id)
        result = await self.session.execute(query)
        reasons = result.scalars().all()

        return [
            {
                "id": r.id,
                "name": r.name,
                "type": {"id": r.type_id, "name": r.type_name} if r.type_id else None,
            }
            for r in reasons
        ]

    def _build_filters(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy filter clauses from filter dict."""
        clauses: list[Any] = []

        if job_id := filters.get("job_id"):
            clauses.append(Application.job_id == job_id)

        if candidate_id := filters.get("candidate_id"):
            clauses.append(Application.candidate_id == candidate_id)

        if status := filters.get("status"):
            clauses.append(Application.status == status)

        if created_before := filters.get("created_before"):
            clauses.append(Application.created_at <= created_before)

        if created_after := filters.get("created_after"):
            clauses.append(Application.created_at >= created_after)

        if updated_before := filters.get("updated_before"):
            clauses.append(Application.updated_at <= updated_before)

        if updated_after := filters.get("updated_after"):
            clauses.append(Application.updated_at >= updated_after)

        return clauses

    def _serialize(self, application: Application) -> dict[str, Any]:
        """Serialize Application model to Harvest API format."""
        candidate = None
        if application.candidate:
            candidate = {
                "id": application.candidate.id,
                "first_name": application.candidate.first_name,
                "last_name": application.candidate.last_name,
                "company": application.candidate.company,
                "title": application.candidate.title,
            }

        job = None
        if application.job:
            job = {
                "id": application.job.id,
                "name": application.job.name,
            }

        current_stage = None
        if application.current_stage:
            current_stage = {
                "id": application.current_stage.id,
                "name": application.current_stage.name,
            }

        rejection_reason = None
        if application.rejection_reason:
            rejection_reason = {
                "id": application.rejection_reason.id,
                "name": application.rejection_reason.name,
                "type": {
                    "id": application.rejection_reason.type_id,
                    "name": application.rejection_reason.type_name,
                }
                if application.rejection_reason.type_id
                else None,
            }

        source = None
        if application.source:
            source = {
                "id": application.source.id,
                "public_name": application.source.public_name,
            }

        answers = [
            {
                "question": a.question,
                "answer": a.answer,
            }
            for a in application.answers
        ]

        return {
            "id": application.id,
            "candidate": candidate,
            "job": job,
            "status": application.status,
            "current_stage": current_stage,
            "rejection_reason": rejection_reason,
            "rejected_at": application.rejected_at,
            "source": source,
            "credited_to": application.credited_to_id,
            "applied_at": application.applied_at,
            "created_at": application.created_at,
            "updated_at": application.updated_at,
            "answers": answers,
        }
