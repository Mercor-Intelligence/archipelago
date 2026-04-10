"""Job Board repository for Greenhouse MCP Server.

Handles data access for public Job Board API entities.
"""

from __future__ import annotations

from typing import Any

from db.models import (
    Degree,
    Discipline,
    Job,
    JobPost,
    JobPostQuestion,
    School,
)
from repositories.base import BaseRepository
from repositories.exceptions import NotFoundError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload


class JobBoardRepository(BaseRepository[JobPost]):
    """Repository for Job Board API operations.

    Implements the public-facing job board endpoints that don't require
    authentication (simulating Greenhouse's public Job Board API).
    """

    model = JobPost

    async def get(self, id: int) -> dict | None:
        """Get a single job post by ID.

        Args:
            id: Job post ID

        Returns:
            Job post data as dict if found, None otherwise
        """
        query = (
            select(JobPost)
            .options(
                selectinload(JobPost.job),
                selectinload(JobPost.questions).selectinload(JobPostQuestion.options),
            )
            .where(JobPost.id == id)
        )
        result = await self.session.execute(query)
        job_post = result.scalar_one_or_none()
        if job_post is None:
            return None
        return self._serialize(job_post)

    async def get_or_raise(self, id: int) -> dict:
        """Get a single job post by ID or raise NotFoundError.

        Args:
            id: Job post ID

        Returns:
            Job post data as dict

        Raises:
            NotFoundError: If job post doesn't exist
        """
        job_post = await self.get(id)
        if job_post is None:
            raise NotFoundError(f"Job post with id {id} does not exist")
        return job_post

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List public job posts with optional filters and pagination.

        Args:
            filters: Optional filter criteria:
                - content: Search in title/content
                - location: Filter by location
                - department_id: Filter by department
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of job post data dicts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = (
            select(JobPost)
            .options(
                selectinload(JobPost.job),
                selectinload(JobPost.questions).selectinload(JobPostQuestion.options),
            )
            .where(JobPost.live.is_(True))  # Only show live posts
            .order_by(JobPost.id)
        )

        if filter_clauses:
            query = query.where(*filter_clauses)

        query = await self._paginate(query, page, per_page)
        result = await self.session.execute(query)
        job_posts = result.scalars().unique().all()

        return [self._serialize(jp) for jp in job_posts]

    async def list_jobs(
        self,
        content: str | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """List public jobs (simplified view for job board).

        This matches the GET /boards/{token}/jobs endpoint.

        Args:
            content: Optional search query
            page: Page number (1-indexed)
            per_page: Number of results per page

        Returns:
            List of simplified job data dicts
        """
        query = select(Job).where(Job.status == "open").order_by(Job.id)

        if content:
            search_pattern = f"%{content}%"
            query = query.where(Job.name.ilike(search_pattern))

        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await self.session.execute(query)
        jobs = result.scalars().all()

        return [self._serialize_job_listing(job) for job in jobs]

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count job posts matching filters.

        Args:
            filters: Optional filter criteria (same as list)

        Returns:
            Number of matching job posts
        """
        filters = filters or {}
        filter_clauses = self._build_filters(filters)

        query = select(func.count()).select_from(JobPost).where(JobPost.live.is_(True))
        if filter_clauses:
            query = query.where(*filter_clauses)

        result = await self.session.scalar(query)
        return result or 0

    async def create(self, data: dict[str, Any]) -> dict:
        """Create a new job post.

        Args:
            data: Job post data with fields:
                - job_id: Job ID (required)
                - title: Post title
                - location: Location string
                - content: Job description HTML
                - live: Whether post is live

        Returns:
            Created job post data as dict
        """
        job_post = JobPost(
            job_id=data["job_id"],
            title=data.get("title"),
            location=data.get("location"),
            content=data.get("content"),
            live=data.get("live", False),
        )
        self.session.add(job_post)
        await self.session.flush()

        return await self.get_or_raise(job_post.id)

    async def update(self, id: int, data: dict[str, Any]) -> dict:
        """Update an existing job post.

        Args:
            id: Job post ID
            data: Updated job post data

        Returns:
            Updated job post data as dict

        Raises:
            NotFoundError: If job post doesn't exist
        """
        job_post = await self._get_by_id_or_raise(id, "JobPost")

        if "title" in data:
            job_post.title = data["title"]
        if "location" in data:
            job_post.location = data["location"]
        if "content" in data:
            job_post.content = data["content"]
        if "live" in data:
            job_post.live = data["live"]

        await self.session.flush()
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """Delete a job post by ID.

        Args:
            id: Job post ID

        Returns:
            True if deleted, False if not found
        """
        job_post = await self._get_by_id(id)
        if job_post is None:
            return False
        await self.session.delete(job_post)
        await self.session.flush()
        return True

    async def get_degrees(self) -> list[dict]:
        """Get all education degrees.

        Returns:
            List of degree data dicts
        """
        query = select(Degree).order_by(Degree.id)
        result = await self.session.execute(query)
        degrees = result.scalars().all()

        return [{"id": d.id, "name": d.name} for d in degrees]

    async def get_disciplines(self) -> list[dict]:
        """Get all education disciplines.

        Returns:
            List of discipline data dicts
        """
        query = select(Discipline).order_by(Discipline.id)
        result = await self.session.execute(query)
        disciplines = result.scalars().all()

        return [{"id": d.id, "name": d.name} for d in disciplines]

    async def get_schools(self) -> list[dict]:
        """Get all schools.

        Returns:
            List of school data dicts
        """
        query = select(School).order_by(School.id)
        result = await self.session.execute(query)
        schools = result.scalars().all()

        return [{"id": s.id, "name": s.name} for s in schools]

    def _build_filters(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy filter clauses from filter dict."""
        clauses: list[Any] = []

        if content := filters.get("content"):
            search_pattern = f"%{content}%"
            clauses.append(JobPost.title.ilike(search_pattern))

        if location := filters.get("location"):
            search_pattern = f"%{location}%"
            clauses.append(JobPost.location.ilike(search_pattern))

        return clauses

    def _serialize(self, job_post: JobPost) -> dict[str, Any]:
        """Serialize JobPost model to Job Board API format."""
        job = None
        if job_post.job:
            job = {
                "id": job_post.job.id,
                "name": job_post.job.name,
            }

        questions = [
            {
                "id": q.id,
                "label": q.label,
                "fields": [
                    {
                        "name": q.field_name,
                        "type": q.field_type,
                        "required": q.required,
                    }
                ],
                "options": [{"id": opt.id, "label": opt.label} for opt in q.options]
                if q.options
                else None,
            }
            for q in job_post.questions
        ]

        return {
            "id": job_post.id,
            "job": job,
            "title": job_post.title,
            "location": {"name": job_post.location},
            "content": job_post.content,
            "internal_job_id": job_post.job_id,
            "live": job_post.live,
            "created_at": job_post.created_at,
            "updated_at": job_post.updated_at,
            "questions": questions,
        }

    def _serialize_job_listing(self, job: Job) -> dict[str, Any]:
        """Serialize Job for public job board listing."""
        return {
            "id": job.id,
            "title": job.name,
            "absolute_url": f"/jobs/{job.id}",
            "updated_at": job.updated_at,
            "requisition_id": job.requisition_id,
            "internal_job_id": job.id,
        }
