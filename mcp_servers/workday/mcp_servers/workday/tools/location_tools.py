"""Location management tools for Workday HCM MCP server."""

from db.models import Location
from db.repositories.location_repository import LocationRepository
from db.session import get_session
from loguru import logger
from mcp_auth import require_roles
from models import CreateLocationInput, LocationOutput
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.decorators import make_async_background

# Error code constants
E_LOC_001 = "E_LOC_001"  # Location not found
E_LOC_002 = "E_LOC_002"  # Location already exists (duplicate)


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_create_location(request: CreateLocationInput) -> LocationOutput:
    """Create a new location in Workday HCM."""
    logger.info(f"Creating location: {request.location_id}")

    repository = LocationRepository()

    with get_session() as session:
        # Check duplicate location_id up front to produce deterministic error
        existing = session.execute(
            select(Location).where(Location.location_id == request.location_id)
        ).scalar_one_or_none()
        if existing:
            raise ValueError(f"{E_LOC_002}: Location already exists: {request.location_id}")

        # Create location via repository
        # Guard against concurrent creation race
        try:
            result = repository.create(session, request)
            logger.info(
                f"Successfully created location: {result.location_id} ({result.location_name})"
            )
            return result
        except IntegrityError as exc:
            # Reset failed transaction state before re-querying
            session.rollback()

            race_existing = session.execute(
                select(Location).where(Location.location_id == request.location_id)
            ).scalar_one_or_none()

            if race_existing is not None:
                # Deterministic duplicate behavior even under concurrency
                raise ValueError(
                    f"{E_LOC_002}: Location already exists: {request.location_id}"
                ) from exc

            # Non-duplicate integrity failures
            raise
