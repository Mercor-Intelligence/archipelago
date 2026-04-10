"""Job profile management tools for Workday HCM."""

from db.models import JobProfile
from db.repositories.job_profile_repository import JobProfileRepository
from db.session import get_session
from loguru import logger
from mcp_auth import require_roles, require_scopes
from models import (
    CreateJobProfileInput,
    GetJobProfileInput,
    JobProfileListOutput,
    JobProfileOutput,
    ListJobProfilesInput,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.decorators import make_async_background

# Error code constants
E_JOB_001 = "E_JOB_001"  # Job profile not found
E_JOB_002 = "E_JOB_002"  # Job profile already exists (duplicate)


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_create_job_profile(request: CreateJobProfileInput) -> JobProfileOutput:
    """Create a new job profile in Workday HCM."""
    logger.info(f"Creating job profile: {request.job_profile_id}")

    repository = JobProfileRepository()

    with get_session() as session:
        # Check duplicate job_profile_id up front to produce deterministic error
        existing = session.execute(
            select(JobProfile).where(JobProfile.job_profile_id == request.job_profile_id)
        ).scalar_one_or_none()
        if existing:
            raise ValueError(f"{E_JOB_002}: Job profile already exists: {request.job_profile_id}")

        # Create job profile via repository
        # Guard against concurrent creation race
        try:
            result = repository.create(session, request)
            logger.info(
                f"Successfully created job profile: {result.job_profile_id} ({result.title})"
            )
            return result
        except IntegrityError as exc:
            # Reset failed transaction state before re-querying
            session.rollback()

            race_existing = session.execute(
                select(JobProfile).where(JobProfile.job_profile_id == request.job_profile_id)
            ).scalar_one_or_none()

            if race_existing is not None:
                # Deterministic duplicate behavior even under concurrency
                raise ValueError(
                    f"{E_JOB_002}: Job profile already exists: {request.job_profile_id}"
                ) from exc

            # Non-duplicate integrity failures
            raise


@make_async_background
@require_scopes("read")
def workday_get_job_profile(request: GetJobProfileInput) -> JobProfileOutput:
    """Retrieve a job profile by ID."""
    # Get job profile from repository
    with get_session() as session:
        repo = JobProfileRepository()
        job_profile = repo.get_job_profile(session, request)

        if not job_profile:
            raise ValueError("E_JOB_001: Job profile not found")

        return job_profile


@make_async_background
@require_scopes("read")
def workday_list_job_profiles(request: ListJobProfilesInput) -> JobProfileListOutput:
    """List job profiles with optional filtering and pagination."""
    logger.info(
        f"Listing job profiles with job_family={request.job_family} "
        f"page={request.page_number} page_size={request.page_size}"
    )

    repository = JobProfileRepository()

    with get_session() as session:
        result = repository.list_job_profiles(session, request)
        logger.info(f"Found {result.total_count} job profiles matching filters")
        return result
