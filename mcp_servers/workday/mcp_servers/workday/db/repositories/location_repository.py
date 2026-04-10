"""LocationRepository for managing location CRUD operations.

This repository handles all database operations for locations.
"""

from models import CreateLocationInput, LocationOutput
from sqlalchemy.orm import Session

from db.models import Location


class LocationRepository:
    """Repository for location database operations."""

    def create(self, session: Session, request: CreateLocationInput) -> LocationOutput:
        """Create a new location.

        Args:
            session: Database session
            request: Location creation request

        Returns:
            Created location details

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        location = Location(
            location_id=request.location_id,
            location_name=request.location_name,
            city=request.city,
            country=request.country,
        )
        session.add(location)
        session.flush()

        return self._to_output(location)

    def _to_output(self, location: Location) -> LocationOutput:
        """Convert Location ORM model to Pydantic output model."""
        return LocationOutput(
            location_id=location.location_id,
            location_name=location.location_name,
            city=location.city,
            country=location.country,
            created_at=location.created_at.isoformat(),
        )
