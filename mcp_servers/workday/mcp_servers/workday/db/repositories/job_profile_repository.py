"""JobProfileRepository for managing job profile CRUD operations.

This repository handles all database operations for job profiles.
"""

from models import (
    CreateJobProfileInput,
    GetJobProfileInput,
    JobProfileListOutput,
    JobProfileOutput,
    ListJobProfilesInput,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import JobProfile


class JobProfileRepository:
    """Repository for job profile database operations."""

    def create(self, session: Session, request: CreateJobProfileInput) -> JobProfileOutput:
        """Create a new job profile.

        Args:
            session: Database session
            request: Job profile creation request

        Returns:
            Created job profile details

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        job_profile = JobProfile(
            job_profile_id=request.job_profile_id,
            title=request.title,
            job_family=request.job_family,
            job_level=request.job_level,
        )
        session.add(job_profile)
        session.flush()

        return self._to_output(job_profile)

    def get_job_profile(
        self, session: Session, request: GetJobProfileInput
    ) -> JobProfileOutput | None:
        """Get job profile by ID.

        Args:
            session: Database session
            request: Get job profile request

        Returns:
            Job profile details if found, None otherwise
        """
        stmt = select(JobProfile).where(JobProfile.job_profile_id == request.job_profile_id)
        result = session.execute(stmt)
        job_profile = result.scalar_one_or_none()

        if not job_profile:
            return None

        return self._to_output(job_profile)

    def list_job_profiles(
        self, session: Session, request: ListJobProfilesInput
    ) -> JobProfileListOutput:
        """List job profiles with optional filtering and pagination."""
        base_query = select(JobProfile)

        if request.job_family:
            base_query = base_query.where(JobProfile.job_family == request.job_family)

        count_stmt = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_stmt).scalar_one()

        offset = (request.page_number - 1) * request.page_size
        # Use secondary sort key (job_profile_id) for deterministic ordering with ties
        stmt = (
            base_query.order_by(JobProfile.created_at.desc(), JobProfile.job_profile_id)
            .offset(offset)
            .limit(request.page_size)
        )

        result = session.execute(stmt)
        job_profiles = list(result.scalars().all())

        return JobProfileListOutput(
            job_profiles=[self._to_output(jp) for jp in job_profiles],
            total_count=total_count,
            page_size=request.page_size,
            page_number=request.page_number,
        )

    def _to_output(self, job_profile: JobProfile) -> JobProfileOutput:
        """Convert JobProfile ORM model to Pydantic output model."""
        return JobProfileOutput(
            job_profile_id=job_profile.job_profile_id,
            title=job_profile.title,
            job_family=job_profile.job_family,
            job_level=job_profile.job_level,
            created_at=job_profile.created_at.isoformat(),
        )
