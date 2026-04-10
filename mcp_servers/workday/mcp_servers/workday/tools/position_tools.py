"""Position management tools for Workday HCM MCP server."""

from db.models import JobProfile, SupervisoryOrg
from db.repositories.position_repository import PositionRepository
from db.session import get_session
from loguru import logger
from mcp_auth import require_roles, require_scopes
from models import (
    ClosePositionInput,
    CreatePositionInput,
    GetPositionInput,
    ListPositionsInput,
    PositionListOutput,
    PositionOutput,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.decorators import make_async_background

# Error code constants
E_JOB_001 = "E_JOB_001"  # Job profile not found
E_ORG_001 = "E_ORG_001"  # Organization not found


def _validate_foreign_keys(session, job_profile_id: str, org_id: str) -> None:
    """Validate that job_profile_id and org_id exist."""
    # Validate job profile exists
    job_profile = session.execute(
        select(JobProfile).where(JobProfile.job_profile_id == job_profile_id)
    ).scalar_one_or_none()
    if not job_profile:
        raise ValueError(f"{E_JOB_001}: Job profile not found")

    # Validate organization exists
    org = session.execute(
        select(SupervisoryOrg).where(SupervisoryOrg.org_id == org_id)
    ).scalar_one_or_none()
    if not org:
        raise ValueError(f"{E_ORG_001}: Organization not found")


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_create_position(request: CreatePositionInput) -> PositionOutput:
    """Create a new position in Workday HCM."""
    repository = PositionRepository()

    with get_session() as session:
        # Validate foreign keys
        _validate_foreign_keys(session, request.job_profile_id, request.org_id)

        # Create position via repository
        # Handle duplicate position_id atomically by catching IntegrityError
        # This ensures atomicity under concurrent requests - the database constraint
        # will catch duplicates even if two requests pass validation simultaneously
        try:
            return repository.create(session, request)
        except IntegrityError as e:
            # Check if this is a unique constraint violation on position_id
            # SQLite: "UNIQUE constraint failed: positions.position_id"
            # PostgreSQL: "duplicate key value violates unique constraint"
            error_str = str(e.orig).lower()
            if (
                "unique constraint" in error_str
                or "duplicate key" in error_str
                or "position_id" in error_str
            ):
                raise ValueError(f"Position already exists: {request.position_id}") from e
            # Re-raise if it's a different integrity error (e.g., foreign key violation)
            raise


@make_async_background
@require_scopes("read")
def workday_get_position(request: GetPositionInput) -> PositionOutput:
    """Retrieve detailed information about a position."""
    repository = PositionRepository()

    with get_session() as session:
        position = repository.get_by_id(session, request)

        if not position:
            raise ValueError("E_POS_001: Position not found")

        return position


@make_async_background
@require_scopes("read")
def workday_list_positions(request: ListPositionsInput) -> PositionListOutput:
    """List positions with filtering and pagination."""
    logger.info(
        f"Listing positions with filters: org_id={request.org_id}, "
        f"status={request.status}, job_profile_id={request.job_profile_id}, "
        f"page={request.page_number}, page_size={request.page_size}"
    )

    with get_session() as session:
        repository = PositionRepository()
        result = repository.list_positions(session, request)
        logger.info(f"Found {result.total_count} positions matching filters")
        return result


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_close_position(request: ClosePositionInput) -> PositionOutput:
    """Close a position, marking it as unavailable for hiring."""
    repository = PositionRepository()

    with get_session() as session:
        return repository.close_position(session, request)
