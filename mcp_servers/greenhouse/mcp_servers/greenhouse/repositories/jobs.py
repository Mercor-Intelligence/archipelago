"""Job repository for Greenhouse MCP Server.

Handles data access for Job entities with Harvest API response formatting.
"""

from __future__ import annotations

from typing import Any

from db.models import (
    HiringTeam,
    Job,
    JobDepartment,
    JobOffice,
    JobStage,
)
from repositories.base import BaseRepository
from repositories.exceptions import NotFoundError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload


class JobRepository(BaseRepository[Job]):
    """Repository for Job entity CRUD operations.

    Implements standard repository pattern for jobs with filtering,
    pagination, and Harvest API response formatting.
    """

    model = Job

    async def get(self, id: int) -> dict | None:
        """Get a single job by ID.

        Args:
            id: Job ID

        Returns:
            Job data as dict if found, None otherwise
        """
        query = (
            select(Job)
            .options(
                selectinload(Job.departments).selectinload(JobDepartment.department),
                selectinload(Job.offices).selectinload(JobOffice.office),
                selectinload(Job.hiring_team).selectinload(HiringTeam.user),
                selectinload(Job.stages),
                selectinload(Job.openings),
            )
            .where(Job.id == id)
        )
        result = await self.session.execute(query)
        job = result.scalar_one_or_none()
        if job is None:
            return None
        return self._serialize(job)

    async def get_or_raise(self, id: int) -> dict:
        """Get a single job by ID or raise NotFoundError.

        Args:
            id: Job ID

        Returns:
            Job data as dict

        Raises:
            NotFoundError: If job doesn't exist
        """
        job = await self.get(id)
        if job is None:
            raise NotFoundError(f"Job with id {id} does not exist")
        return job

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List jobs with optional filters and pagination.

        Args:
            filters: Optional filter criteria:
                - status: Filter by status (open, closed, draft)
                - department_id: Filter by department
                - office_id: Filter by office
                - created_before: ISO 8601 timestamp
                - created_after: ISO 8601 timestamp
                - updated_before: ISO 8601 timestamp
                - updated_after: ISO 8601 timestamp
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of job data dicts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = (
            select(Job)
            .options(
                selectinload(Job.departments).selectinload(JobDepartment.department),
                selectinload(Job.offices).selectinload(JobOffice.office),
                selectinload(Job.hiring_team).selectinload(HiringTeam.user),
                selectinload(Job.stages),
                selectinload(Job.openings),
            )
            .order_by(Job.id)
        )

        if filter_clauses:
            query = query.where(*filter_clauses)

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        jobs = result.scalars().unique().all()

        return [self._serialize(job) for job in jobs]

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count jobs matching filters.

        Args:
            filters: Optional filter criteria (same as list)

        Returns:
            Number of matching jobs
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = select(func.count()).select_from(Job)
        if filter_clauses:
            query = query.where(*filter_clauses)

        result = await self.session.scalar(query)
        return result or 0

    async def get_stages(self, job_id: int) -> list[dict]:
        """Get pipeline stages for a job.

        Args:
            job_id: Job ID

        Returns:
            List of stage data dicts

        Raises:
            NotFoundError: If job doesn't exist
        """
        job = await self._get_by_id(job_id)
        if job is None:
            raise NotFoundError(f"Job with id {job_id} does not exist")

        query = select(JobStage).where(JobStage.job_id == job_id).order_by(JobStage.priority)
        result = await self.session.execute(query)
        stages = result.scalars().all()

        return [self._serialize_stage(stage) for stage in stages]

    async def create(self, data: dict[str, Any]) -> dict:
        """Create a new job.

        Args:
            data: Job data with fields:
                - name: Job name (required)
                - requisition_id: Optional requisition ID
                - notes: Optional notes
                - confidential: Optional confidentiality flag
                - status: Status (draft, open, closed)
                - department_ids: Optional list of department IDs
                - office_ids: Optional list of office IDs

        Returns:
            Created job data as dict
        """
        job = Job(
            name=data["name"],
            requisition_id=data.get("requisition_id"),
            notes=data.get("notes"),
            confidential=data.get("confidential", False),
            status=data.get("status", "draft"),
            is_template=data.get("is_template", False),
        )
        self.session.add(job)
        await self.session.flush()

        # Create default pipeline stages
        default_stages = [
            ("Application Review", 0),
            ("Phone Screen", 1),
            ("Technical Interview", 2),
            ("Onsite Interview", 3),
            ("Offer", 4),
        ]
        for name, priority in default_stages:
            stage = JobStage(job_id=job.id, name=name, priority=priority)
            self.session.add(stage)

        await self.session.flush()
        return await self.get_or_raise(job.id)

    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing job.

        Args:
            id: Job ID
            data: Updated job data

        Returns:
            Updated job data as dict

        Raises:
            NotFoundError: If job doesn't exist
        """
        job = await self._get_by_id_or_raise(id, "Job")

        if "name" in data:
            job.name = data["name"]
        if "requisition_id" in data:
            job.requisition_id = data["requisition_id"]
        if "notes" in data:
            job.notes = data["notes"]
        if "confidential" in data:
            job.confidential = data["confidential"]
        if "status" in data:
            job.status = data["status"]

        await self.session.flush()
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """Delete a job by ID.

        Args:
            id: Job ID

        Returns:
            True if deleted, False if not found
        """
        job = await self._get_by_id(id)
        if job is None:
            return False
        await self.session.delete(job)
        await self.session.flush()
        return True

    def _build_filters(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy filter clauses from filter dict."""
        clauses: list[Any] = []

        if status := filters.get("status"):
            clauses.append(Job.status == status)

        if created_before := filters.get("created_before"):
            clauses.append(Job.created_at <= created_before)

        if created_after := filters.get("created_after"):
            clauses.append(Job.created_at >= created_after)

        if updated_before := filters.get("updated_before"):
            clauses.append(Job.updated_at <= updated_before)

        if updated_after := filters.get("updated_after"):
            clauses.append(Job.updated_at >= updated_after)

        return clauses

    def _serialize(self, job: Job) -> dict[str, Any]:
        """Serialize Job model to Harvest API format."""
        departments = []
        for assoc in job.departments:
            if assoc.department:
                departments.append(
                    {
                        "id": assoc.department.id,
                        "name": assoc.department.name,
                        "parent_id": assoc.department.parent_id,
                        "external_id": assoc.department.external_id,
                    }
                )

        offices = []
        for assoc in job.offices:
            if assoc.office:
                offices.append(
                    {
                        "id": assoc.office.id,
                        "name": assoc.office.name,
                        "location": {"name": assoc.office.location_name},
                        "parent_id": assoc.office.parent_id,
                        "external_id": assoc.office.external_id,
                    }
                )

        hiring_team = {
            "hiring_managers": [],
            "recruiters": [],
            "coordinators": [],
            "sourcers": [],
        }
        for member in job.hiring_team:
            if member.user:
                user_data = {
                    "id": member.user.id,
                    "first_name": member.user.first_name,
                    "last_name": member.user.last_name,
                    "name": member.user.name,
                    "employee_id": member.user.employee_id,
                    "responsible": member.responsible,
                }
                role_key = f"{member.role}s" if not member.role.endswith("s") else member.role
                if role_key in hiring_team:
                    hiring_team[role_key].append(user_data)

        openings = [
            {
                "id": opening.id,
                "opening_id": opening.opening_id,
                "status": opening.status,
                "opened_at": opening.opened_at,
                "closed_at": opening.closed_at,
                "application_id": opening.application_id,
                "close_reason": (
                    {"id": opening.close_reason_id} if opening.close_reason_id else None
                ),
            }
            for opening in job.openings
        ]

        return {
            "id": job.id,
            "name": job.name,
            "requisition_id": job.requisition_id,
            "notes": job.notes,
            "confidential": job.confidential,
            "status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "opened_at": job.opened_at,
            "closed_at": job.closed_at,
            "is_template": job.is_template,
            "copied_from_id": job.copied_from_id,
            "departments": departments,
            "offices": offices,
            "hiring_team": hiring_team,
            "openings": openings,
        }

    def _serialize_stage(self, stage: JobStage) -> dict[str, Any]:
        """Serialize JobStage model to Harvest API format."""
        return {
            "id": stage.id,
            "name": stage.name,
            "priority": stage.priority,
            "job_id": stage.job_id,
            "created_at": stage.created_at,
            "updated_at": stage.updated_at,
        }
