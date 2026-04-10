"""WorkerRepository for managing worker CRUD operations.

This repository handles all database operations for workers, including
temporal queries, lifecycle events, and position assignments.
"""

from uuid import uuid4

from models import (
    CreateWorkerInput,
    GetWorkerInput,
    ListWorkersInput,
    TerminateWorkerInput,
    TransferWorkerInput,
    WorkerListOutput,
    WorkerOutput,
)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from db.models import Movement, Position, Worker


class WorkerRepository:
    """Repository for worker database operations."""

    def create(self, session: Session, request: CreateWorkerInput) -> WorkerOutput:
        """Create a new worker (hire event).

        Args:
            session: Database session
            request: Worker creation request

        Returns:
            Created worker details

        Raises:
            ValueError: If validation fails

        Note:
            Does not commit the transaction. Caller is responsible for committing.
        """
        # Set effective_date to hire_date if not provided
        effective_date = request.effective_date or request.hire_date

        # If position_id is provided, validate it exists and is open
        # Lock the position to prevent race conditions:
        # - PostgreSQL/MySQL: with_for_update() acquires row-level lock
        # - SQLite: with_for_update() is ignored, but isolation_level="SERIALIZABLE"
        #   on the engine ensures transactions behave as if executed serially
        position = None
        if request.position_id:
            position = session.execute(
                select(Position)
                .where(Position.position_id == request.position_id)
                .with_for_update()
            ).scalar_one_or_none()

            if not position:
                raise ValueError(f"Position {request.position_id} not found")
            if position.status != "open":
                raise ValueError(f"Position {request.position_id} is not open")

        # Create worker
        worker = Worker(
            worker_id=request.worker_id,
            job_profile_id=request.job_profile_id,
            org_id=request.org_id,
            cost_center_id=request.cost_center_id,
            location_id=request.location_id,
            position_id=request.position_id,
            employment_status="Active",
            fte=request.fte,
            hire_date=request.hire_date,
            effective_date=effective_date,
        )
        session.add(worker)
        session.flush()

        # If position assigned, update position to filled
        if position:
            position.status = "filled"
            position.worker_id = request.worker_id

        # Create hire movement event
        movement = Movement(
            event_id=str(uuid4()),
            worker_id=request.worker_id,
            event_type="hire",
            event_date=request.hire_date,
            to_org_id=request.org_id,
            to_cost_center_id=request.cost_center_id,
            to_job_profile_id=request.job_profile_id,
            to_position_id=request.position_id,
        )
        session.add(movement)
        session.flush()

        return self._to_output(worker)

    def get_by_id(self, session: Session, request: GetWorkerInput) -> WorkerOutput | None:
        """Get worker by ID with optional temporal query.

        Args:
            session: Database session
            request: Get worker request

        Returns:
            Worker details if found, None otherwise
        """
        stmt = select(Worker).where(Worker.worker_id == request.worker_id)

        # Apply temporal filter if as_of_date provided
        # Show workers that were active on that date: hired by then, not yet terminated
        if request.as_of_date:
            stmt = stmt.where(
                Worker.hire_date <= request.as_of_date,
                or_(
                    Worker.termination_date.is_(None), Worker.termination_date > request.as_of_date
                ),
            )

        result = session.execute(stmt)
        worker = result.scalar_one_or_none()

        if not worker:
            return None

        return self._to_output(worker)

    def list_workers(self, session: Session, request: ListWorkersInput) -> WorkerListOutput:
        """List workers with pagination and filters.

        Args:
            session: Database session
            request: List workers request

        Returns:
            Paginated list of workers
        """
        # Build base query
        base_query = select(Worker)

        # Apply filters
        if request.org_id:
            base_query = base_query.where(Worker.org_id == request.org_id)
        if request.cost_center_id:
            base_query = base_query.where(Worker.cost_center_id == request.cost_center_id)
        if request.employment_status:
            base_query = base_query.where(Worker.employment_status == request.employment_status)
        # Apply temporal filter: show workers that were active on that date
        if request.as_of_date:
            base_query = base_query.where(
                Worker.hire_date <= request.as_of_date,
                or_(
                    Worker.termination_date.is_(None), Worker.termination_date > request.as_of_date
                ),
            )

        # Get total count
        count_stmt = select(func.count()).select_from(base_query.subquery())
        total_count = session.execute(count_stmt).scalar_one()

        # Apply pagination
        # Use secondary sort key (worker_id) for deterministic ordering with ties
        offset = (request.page_number - 1) * request.page_size
        stmt = (
            base_query.order_by(Worker.created_at.desc(), Worker.worker_id)
            .offset(offset)
            .limit(request.page_size)
        )

        # Execute query
        result = session.execute(stmt)
        workers = list(result.scalars().all())

        return WorkerListOutput(
            workers=[self._to_output(w) for w in workers],
            total_count=total_count,
            page_size=request.page_size,
            page_number=request.page_number,
        )

    def terminate(self, session: Session, request: TerminateWorkerInput) -> WorkerOutput:
        """Update a worker's employment status (Terminated or Leave).

        Args:
            session: Database session
            request: Status update request

        Returns:
            Updated worker details

        Raises:
            ValueError: If validation fails

        Note:
            Uses SELECT ... FOR UPDATE to prevent concurrent status updates
            creating duplicate events or inconsistent state.
        """
        from datetime import datetime

        # Get worker with row lock to prevent concurrent status updates
        # - PostgreSQL/MySQL: with_for_update() acquires row-level lock
        # - SQLite: with_for_update() ignored, but SERIALIZABLE isolation level
        #   (configured in db/session.py) ensures serial transaction execution
        worker = session.execute(
            select(Worker).where(Worker.worker_id == request.worker_id).with_for_update()
        ).scalar_one_or_none()

        if not worker:
            raise ValueError(f"Worker {request.worker_id} not found")

        # Repository enforces business rule: only Active workers can have status changed
        # This prevents bypass via direct repository calls or race conditions
        if worker.employment_status == request.new_status:
            status_name = "terminated" if request.new_status == "Terminated" else "on leave"
            raise ValueError(f"Worker is already {status_name}")
        if worker.employment_status != "Active":
            raise ValueError(
                f"Worker must be Active to change status (current status: {worker.employment_status})"
            )

        # Parse dates for proper semantic comparison
        # Note: request.status_date already strictly validated in worker_tools.py
        # But repositories should be defensive - validate inputs independently
        try:
            status_dt = datetime.strptime(request.status_date, "%Y-%m-%d")
            # Ensure strict YYYY-MM-DD format (reject non-zero-padded)
            if status_dt.strftime("%Y-%m-%d") != request.status_date:
                raise ValueError("Invalid date format. Use YYYY-MM-DD")
        except ValueError as e:
            if "Invalid date format" in str(e):
                raise
            raise ValueError("Invalid date format. Use YYYY-MM-DD") from e

        # Validate effective_date format if provided
        if request.effective_date:
            try:
                effective_dt = datetime.strptime(request.effective_date, "%Y-%m-%d")
                if effective_dt.strftime("%Y-%m-%d") != request.effective_date:
                    raise ValueError("Invalid date format. Use YYYY-MM-DD")
            except ValueError as e:
                if "Invalid date format" in str(e):
                    raise
                raise ValueError("Invalid date format. Use YYYY-MM-DD") from e

        # Parse hire_date from database (lenient - supports multiple legacy formats)
        # Per checklist: be lenient with existing data, strict with new inputs
        # IMPORTANT: Only use UNAMBIGUOUS formats (year-first) to avoid misinterpretation
        # Formats like MM/DD/YYYY vs DD/MM/YYYY are excluded as they're semantically ambiguous
        # NO dateutil fallback - prevents locale-dependent ambiguous parsing (e.g., 01/02/2025)
        hire_dt = None
        date_formats = [
            "%Y-%m-%d",  # Standard: 2025-01-15 or 2025-1-15 (strptime is flexible)
            "%Y/%m/%d",  # Year-first with slashes: 2025/01/15
            "%Y-%m-%dT%H:%M:%S",  # ISO 8601 with time: 2025-01-15T00:00:00
            "%Y-%m-%d %H:%M:%S",  # SQL datetime: 2025-01-15 00:00:00
            "%Y-%m-%dT%H:%M:%S.%f",  # ISO with microseconds: 2025-01-15T00:00:00.000000
        ]

        for fmt in date_formats:
            try:
                hire_dt = datetime.strptime(worker.hire_date, fmt)
                break  # Successfully parsed
            except ValueError:
                continue  # Try next format

        # If all formats fail, reject the value
        if hire_dt is None:
            error_msg = (
                f"Cannot parse hire_date for worker {request.worker_id}: '{worker.hire_date}'. "
                f"Database value is in an unrecognized format. Supported formats: "
                f"YYYY-MM-DD, YYYY/MM/DD, ISO 8601 timestamps (YYYY-MM-DDTHH:MM:SS[.fff])."
            )
            raise ValueError(error_msg)

        # Date comparison: normalize to midnight to compare only dates (not times)
        # This prevents blocking same-day status changes when hire_date has a time component
        status_date_only = status_dt.date()
        hire_date_only = hire_dt.date()

        if status_date_only < hire_date_only:
            # Use "Termination date" in error message for backward compatibility
            raise ValueError("Termination date cannot be before hire date")

        # Set effective_date to status_date if not provided
        effective_date = request.effective_date or request.status_date

        # Update worker employment status
        worker.employment_status = request.new_status

        # Only set termination_date for Terminated status
        if request.new_status == "Terminated":
            worker.termination_date = request.status_date

        worker.effective_date = effective_date

        # Capture position_id before potentially clearing it for movement event
        from_position_id = worker.position_id

        # If worker has position, free it (for both Terminated and Leave)
        if worker.position_id:
            position = session.get(Position, worker.position_id)
            if position:
                position.status = "open"
                position.worker_id = None
            worker.position_id = None

        session.flush()

        # Create movement event
        movement = Movement(
            event_id=str(uuid4()),
            worker_id=request.worker_id,
            event_type="termination",  # Use "termination" for both Leave and Terminated
            event_date=request.status_date,
            from_org_id=worker.org_id,
            from_cost_center_id=worker.cost_center_id,
            from_job_profile_id=worker.job_profile_id,
            from_position_id=from_position_id,
        )
        session.add(movement)
        session.flush()

        return self._to_output(worker)

    def transfer_worker(self, session: Session, request: TransferWorkerInput) -> WorkerOutput:
        """Transfer a worker to new organization/position/etc.

        Args:
            session: Database session
            request: Transfer request

        Returns:
            Updated worker details

        Raises:
            ValueError: If validation fails
        """
        # Get worker
        worker = session.execute(
            select(Worker).where(Worker.worker_id == request.worker_id)
        ).scalar_one_or_none()

        if not worker:
            raise ValueError(f"Worker {request.worker_id} not found")
        if worker.employment_status != "Active":
            raise ValueError("Can only transfer active workers")

        # Validate at least one field to update
        if not any(
            [
                request.new_org_id,
                request.new_cost_center_id,
                request.new_job_profile_id,
                request.new_position_id,
                request.new_fte is not None,
            ]
        ):
            raise ValueError("At least one field must be provided for transfer")

        # Set effective_date to transfer_date if not provided
        effective_date = request.effective_date or request.transfer_date

        # Store old values for movement event
        from_org_id = worker.org_id
        from_cost_center_id = worker.cost_center_id
        from_job_profile_id = worker.job_profile_id
        from_position_id = worker.position_id

        # Update worker fields
        if request.new_org_id:
            worker.org_id = request.new_org_id
        if request.new_cost_center_id:
            worker.cost_center_id = request.new_cost_center_id
        if request.new_job_profile_id:
            worker.job_profile_id = request.new_job_profile_id
        if request.new_fte is not None:
            worker.fte = request.new_fte
        worker.effective_date = effective_date

        # Handle position change
        if request.new_position_id:
            # Free old position if any
            if worker.position_id:
                old_position = session.get(Position, worker.position_id)
                if old_position:
                    old_position.status = "open"
                    old_position.worker_id = None

            # Assign new position - lock to prevent race conditions:
            # - PostgreSQL/MySQL: with_for_update() acquires row-level lock
            # - SQLite: with_for_update() is ignored, but isolation_level="SERIALIZABLE"
            #   on the engine ensures transactions behave as if executed serially
            new_position = session.execute(
                select(Position)
                .where(Position.position_id == request.new_position_id)
                .with_for_update()
            ).scalar_one_or_none()

            if not new_position:
                raise ValueError(f"Position {request.new_position_id} not found")
            if new_position.status != "open":
                raise ValueError(f"Position {request.new_position_id} is not open")

            new_position.status = "filled"
            new_position.worker_id = request.worker_id
            worker.position_id = request.new_position_id

        session.flush()

        # Create transfer movement event
        movement = Movement(
            event_id=str(uuid4()),
            worker_id=request.worker_id,
            event_type="transfer",
            event_date=request.transfer_date,
            from_org_id=from_org_id,
            to_org_id=request.new_org_id or from_org_id,
            from_cost_center_id=from_cost_center_id,
            to_cost_center_id=request.new_cost_center_id or from_cost_center_id,
            from_job_profile_id=from_job_profile_id,
            to_job_profile_id=request.new_job_profile_id or from_job_profile_id,
            from_position_id=from_position_id,
            to_position_id=request.new_position_id or from_position_id,
        )
        session.add(movement)
        session.flush()

        return self._to_output(worker)

    def _to_output(self, worker: Worker) -> WorkerOutput:
        """Convert Worker ORM model to Pydantic output model."""
        return WorkerOutput(
            worker_id=worker.worker_id,
            job_profile_id=worker.job_profile_id,
            org_id=worker.org_id,
            cost_center_id=worker.cost_center_id,
            location_id=worker.location_id,
            position_id=worker.position_id,
            employment_status=worker.employment_status,
            fte=worker.fte,
            hire_date=worker.hire_date,
            termination_date=worker.termination_date,
            effective_date=worker.effective_date,
            created_at=worker.created_at.isoformat(),
            updated_at=worker.updated_at.isoformat(),
        )
