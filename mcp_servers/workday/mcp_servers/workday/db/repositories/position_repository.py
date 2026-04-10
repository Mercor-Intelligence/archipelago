"""PositionRepository for managing position CRUD operations.

This repository handles all database operations for positions, including
position lifecycle (open/filled/closed) and assignments.
"""

from models import (
    ClosePositionInput,
    CreatePositionInput,
    GetPositionInput,
    ListPositionsInput,
    PositionListOutput,
    PositionOutput,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Position


class PositionRepository:
    """Repository for position database operations."""

    def create(self, session: Session, request: CreatePositionInput) -> PositionOutput:
        """Create a new position.

        Args:
            session: Database session
            request: Position creation request

        Returns:
            Created position details

        Raises:
            ValueError: If validation fails

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        # Validate that only open positions can be created
        if request.status != "open":
            raise ValueError("New positions must be created with status 'open'")

        # Create position
        position = Position(
            position_id=request.position_id,
            job_profile_id=request.job_profile_id,
            org_id=request.org_id,
            fte=request.fte,
            status=request.status,
            worker_id=None,  # New positions start unassigned
        )
        session.add(position)
        session.flush()

        return self._to_output(position)

    def get_by_id(self, session: Session, request: GetPositionInput) -> PositionOutput | None:
        """Get position by ID.

        Args:
            session: Database session
            request: Get position request

        Returns:
            Position details if found, None otherwise
        """
        stmt = select(Position).where(Position.position_id == request.position_id)
        result = session.execute(stmt)
        position = result.scalar_one_or_none()

        if not position:
            return None

        return self._to_output(position)

    def list_positions(self, session: Session, request: ListPositionsInput) -> PositionListOutput:
        """List positions with pagination and filters.

        Args:
            session: Database session
            request: List positions request

        Returns:
            Paginated list of positions
        """
        # Build base query
        base_query = select(Position)

        # Apply filters
        if request.org_id:
            base_query = base_query.where(Position.org_id == request.org_id)
        if request.status:
            base_query = base_query.where(Position.status == request.status)
        if request.job_profile_id:
            base_query = base_query.where(Position.job_profile_id == request.job_profile_id)

        # Get total count
        count_stmt = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_stmt).scalar_one()

        # Apply pagination
        # Use secondary sort key (position_id) for deterministic ordering with ties
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            base_query.order_by(Position.created_at.desc(), Position.position_id)
            .offset(offset)
            .limit(request.page_size)
        )

        # Execute query
        result = session.execute(stmt)
        positions = list(result.scalars().all())

        return PositionListOutput(
            positions=[self._to_output(p) for p in positions],
            total_count=total_count,
            page_size=request.page_size,
            page_number=request.page_number,
        )

    def close_position(self, session: Session, request: ClosePositionInput) -> PositionOutput:
        """Close/freeze a position.

        Args:
            session: Database session
            request: Close position request

        Returns:
            Updated position details

        Raises:
            ValueError: If validation fails
        """
        # Get position
        position = session.execute(
            select(Position).where(Position.position_id == request.position_id)
        ).scalar_one_or_none()

        if not position:
            raise ValueError("E_POS_001: Position not found")
        if position.status == "filled":
            raise ValueError("Cannot close filled position. Terminate worker first.")

        # Update status
        position.status = "closed"
        session.flush()

        return self._to_output(position)

    def _to_output(self, position: Position) -> PositionOutput:
        """Convert Position ORM model to Pydantic output model."""
        return PositionOutput(
            position_id=position.position_id,
            job_profile_id=position.job_profile_id,
            org_id=position.org_id,
            fte=position.fte,
            status=position.status,
            worker_id=position.worker_id,
            created_at=position.created_at.isoformat(),
            updated_at=position.updated_at.isoformat(),
        )
