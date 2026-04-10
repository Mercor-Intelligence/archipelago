"""Worker management tools for Workday HCM."""

from datetime import datetime

from db.models import CostCenter, JobProfile, Location, Position, SupervisoryOrg, Worker
from db.repositories.worker_repository import WorkerRepository
from db.session import get_session
from mcp_auth import require_roles, require_scopes
from models import (
    CreateWorkerInput,
    GetWorkerInput,
    ListWorkersInput,
    TerminateWorkerInput,
    TransferWorkerInput,
    WorkerListOutput,
    WorkerOutput,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.decorators import make_async_background

# Error code constants (per BUILD_PLAN.md § 2.7)
E_WRK_001 = "E_WRK_001"  # Worker not found
E_WRK_002 = "E_WRK_002"  # Invalid employment status
E_JOB_001 = "E_JOB_001"  # Job profile not found
E_ORG_001 = "E_ORG_001"  # Organization not found
E_CC_001 = "E_CC_001"  # Cost center not found
E_LOC_001 = "E_LOC_001"  # Location not found
E_POS_001 = "E_POS_001"  # Position not found
E_POS_002 = "E_POS_002"  # Position already filled


def _validate_foreign_keys(
    session,
    job_profile_id: str,
    org_id: str,
    cost_center_id: str,
    location_id: str | None = None,
    position_id: str | None = None,
) -> Position | None:
    """Validate all foreign keys exist before creating a worker."""
    # Validate job profile exists
    job_profile = session.execute(
        select(JobProfile).where(JobProfile.job_profile_id == job_profile_id)
    ).scalar_one_or_none()
    if not job_profile:
        raise ValueError(f"{E_JOB_001}: Job profile '{job_profile_id}' not found")

    # Validate organization exists
    org = session.execute(
        select(SupervisoryOrg).where(SupervisoryOrg.org_id == org_id)
    ).scalar_one_or_none()
    if not org:
        raise ValueError(f"{E_ORG_001}: Organization '{org_id}' not found")

    # Validate cost center exists
    cost_center = session.execute(
        select(CostCenter).where(CostCenter.cost_center_id == cost_center_id)
    ).scalar_one_or_none()
    if not cost_center:
        raise ValueError(f"{E_CC_001}: Cost center '{cost_center_id}' not found")

    # Validate location if provided
    if location_id:
        location = session.execute(
            select(Location).where(Location.location_id == location_id)
        ).scalar_one_or_none()
        if not location:
            raise ValueError(f"{E_LOC_001}: Location '{location_id}' not found")

    # Validate position if provided
    position = None
    if position_id:
        position = session.execute(
            select(Position).where(Position.position_id == position_id).with_for_update()
        ).scalar_one_or_none()
        if not position:
            raise ValueError(f"{E_POS_001}: Position '{position_id}' not found")
        if position.status != "open":
            raise ValueError(
                f"{E_POS_002}: Position '{position_id}' is already filled "
                f"(status: {position.status})"
            )

    return position


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_hire_worker(request: CreateWorkerInput) -> WorkerOutput:
    """Hire a new worker in Workday HCM."""
    repository = WorkerRepository()

    with get_session() as session:
        # Check duplicate worker_id up front to produce deterministic error
        existing = session.execute(
            select(Worker).where(Worker.worker_id == request.worker_id)
        ).scalar_one_or_none()
        if existing:
            raise ValueError(f"Worker already exists: {request.worker_id}")

        # Validate all foreign keys
        _validate_foreign_keys(
            session,
            job_profile_id=request.job_profile_id,
            org_id=request.org_id,
            cost_center_id=request.cost_center_id,
            location_id=request.location_id,
            position_id=request.position_id,
        )

        # Create worker via repository (handles movement events, position updates).
        # Guard against a concurrent hire race:
        # - If another transaction inserts the same worker_id between our pre-check
        #   and this insert, we'll see an IntegrityError from the unique constraint.
        # - In that case, re-check for the worker and convert to the documented
        #   ValueError duplicate contract.
        try:
            return repository.create(session, request)
        except IntegrityError as exc:
            # Reset failed transaction state before re-querying.
            session.rollback()

            race_existing = session.execute(
                select(Worker).where(Worker.worker_id == request.worker_id)
            ).scalar_one_or_none()

            if race_existing is not None:
                # Deterministic duplicate behavior even under concurrency.
                raise ValueError(f"Worker already exists: {request.worker_id}") from exc

            # Non-duplicate integrity failures (e.g., FK/check constraints)
            # should surface as-is for debugging.
            raise


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_terminate_worker(request: TerminateWorkerInput) -> WorkerOutput:
    """Update a worker's employment status (Terminated or Leave) in Workday HCM."""
    from datetime import datetime

    # Validate date formats (per BUILD_PLAN § 3.2 edge cases)
    # Strict YYYY-MM-DD validation: must be parseable AND match format when re-formatted
    try:
        parsed_status_date = datetime.strptime(request.status_date, "%Y-%m-%d")
        if parsed_status_date.strftime("%Y-%m-%d") != request.status_date:
            raise ValueError("Invalid date format. Use YYYY-MM-DD")

        if request.effective_date:
            parsed_eff_date = datetime.strptime(request.effective_date, "%Y-%m-%d")
            if parsed_eff_date.strftime("%Y-%m-%d") != request.effective_date:
                raise ValueError("Invalid date format. Use YYYY-MM-DD")
    except ValueError as e:
        if "Invalid date format" in str(e):
            raise
        raise ValueError("Invalid date format. Use YYYY-MM-DD") from e

    repository = WorkerRepository()

    with get_session() as session:
        # Get worker to validate existence and status
        # Note: Repository.terminate() will re-fetch with locking for concurrency safety
        worker = session.execute(
            select(Worker).where(Worker.worker_id == request.worker_id)
        ).scalar_one_or_none()

        if not worker:
            raise ValueError(f"{E_WRK_001}: Worker '{request.worker_id}' not found")

        # Validate worker is currently Active (per BUILD_PLAN § 3.2)
        if worker.employment_status != "Active":
            status_name = "terminated" if request.new_status == "Terminated" else "on leave"
            if worker.employment_status == request.new_status:
                raise ValueError(
                    f"{E_WRK_002}: Worker '{request.worker_id}' is already {status_name}"
                )
            else:
                raise ValueError(
                    f"{E_WRK_002}: Worker '{request.worker_id}' must be Active to update status "
                    f"(current status: {worker.employment_status})"
                )

        # Update worker status via repository (handles locking, movement events, position updates)
        # Handle race conditions: worker could be terminated/deleted/status-changed between
        # pre-check and repository lock
        try:
            return repository.terminate(session, request)
        except ValueError as e:
            error_msg = str(e)

            # Map repository ValueErrors to consistent error format per BUILD_PLAN.md
            if "not found" in error_msg:
                # Race: Worker deleted between pre-check and repository lock
                raise ValueError(f"{E_WRK_001}: Worker '{request.worker_id}' not found") from e
            elif (
                "already terminated" in error_msg
                or "must be Active" in error_msg
                or "already on leave" in error_msg
            ):
                # Race: Worker status changed between pre-check and repository lock
                # Both cases are E_WRK_002 (Invalid employment status)
                raise ValueError(f"{E_WRK_002}: {error_msg}") from e
            else:
                # Date validation errors - re-raise as-is (ValueError with descriptive message)
                raise


def _validate_date_format(date_str: str) -> None:
    """Validate date string is in YYYY-MM-DD format."""
    if not date_str or not isinstance(date_str, str):
        raise ValueError("Invalid date format. Use YYYY-MM-DD")

    # Check basic format first (must be exactly 10 characters: YYYY-MM-DD)
    if len(date_str) != 10:
        raise ValueError("Invalid date format. Use YYYY-MM-DD")

    # Check that separators are in the right place
    if date_str[4] != "-" or date_str[7] != "-":
        raise ValueError("Invalid date format. Use YYYY-MM-DD")

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Invalid date format. Use YYYY-MM-DD")


@make_async_background
@require_scopes("read")
def workday_get_worker(request: GetWorkerInput) -> WorkerOutput:
    """Retrieve detailed information about a worker by ID."""
    # Validate as_of_date format if provided
    if request.as_of_date is not None:
        _validate_date_format(request.as_of_date)

    # Get worker from repository
    with get_session() as session:
        repo = WorkerRepository()
        worker = repo.get_by_id(session, request)

        if not worker:
            raise ValueError(f"{E_WRK_001}: Worker not found")

        return worker


@make_async_background
@require_scopes("read")
def workday_list_workers(request: ListWorkersInput) -> WorkerListOutput:
    """List workers with pagination and filtering."""
    # Validate as_of_date format if provided
    if request.as_of_date is not None:
        _validate_date_format(request.as_of_date)

    # Get workers from repository
    with get_session() as session:
        repo = WorkerRepository()
        return repo.list_workers(session, request)


def _get_worker_or_raise(session, worker_id: str) -> Worker:
    """Get worker by ID or raise if not found."""
    worker = session.execute(
        select(Worker).where(Worker.worker_id == worker_id)
    ).scalar_one_or_none()

    if not worker:
        raise ValueError(f"{E_WRK_001}: Worker '{worker_id}' not found")

    return worker


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_transfer_worker(request: TransferWorkerInput) -> WorkerOutput:
    """Transfer a worker to a new organization, cost center, job profile, or position."""
    # Validate date formats up front so we never persist malformed dates
    _validate_date_format(request.transfer_date)
    if request.effective_date is not None:
        _validate_date_format(request.effective_date)

    repository = WorkerRepository()

    with get_session() as session:
        # Check worker exists and is Active
        worker = _get_worker_or_raise(session, request.worker_id)
        if worker.employment_status != "Active":
            raise ValueError(
                f"{E_WRK_002}: Worker '{request.worker_id}' is not Active "
                f"(status: {worker.employment_status})"
            )

        # Validate new foreign keys if provided
        if request.new_job_profile_id:
            job_profile = session.execute(
                select(JobProfile).where(JobProfile.job_profile_id == request.new_job_profile_id)
            ).scalar_one_or_none()
            if not job_profile:
                raise ValueError(
                    f"{E_JOB_001}: Job profile '{request.new_job_profile_id}' not found"
                )

        if request.new_org_id:
            org = session.execute(
                select(SupervisoryOrg).where(SupervisoryOrg.org_id == request.new_org_id)
            ).scalar_one_or_none()
            if not org:
                raise ValueError(f"{E_ORG_001}: Organization '{request.new_org_id}' not found")

        if request.new_cost_center_id:
            cost_center = session.execute(
                select(CostCenter).where(CostCenter.cost_center_id == request.new_cost_center_id)
            ).scalar_one_or_none()
            if not cost_center:
                raise ValueError(
                    f"{E_CC_001}: Cost center '{request.new_cost_center_id}' not found"
                )

        # Validate new position if provided (check exists and is open with lock)
        if request.new_position_id:
            position = session.execute(
                select(Position)
                .where(Position.position_id == request.new_position_id)
                .with_for_update()
            ).scalar_one_or_none()
            if not position:
                raise ValueError(f"{E_POS_001}: Position '{request.new_position_id}' not found")
            # Position must be open OR already held by this worker.
            # Repository logic first frees the worker's current position (if any),
            # then locks and assigns the new position. Allowing the "same filled
            # position" case here keeps tool-level validation aligned with that behavior.
            if position.status != "open" and position.worker_id != request.worker_id:
                # Distinguish between "filled" (classic E_POS_002 case) and other
                # non-open statuses such as "closed" so we don't misreport state.
                if position.status == "filled":
                    raise ValueError(
                        f"{E_POS_002}: Position '{request.new_position_id}' is already filled "
                        f"(status: {position.status})"
                    )
                else:
                    raise ValueError(
                        f"{E_POS_002}: Position '{request.new_position_id}' is not open "
                        f"(status: {position.status})"
                    )

        # Transfer worker via repository (handles all business logic)
        try:
            return repository.transfer_worker(session, request)
        except ValueError as e:
            # Repository raised ValueError - translate to consistent error format
            # This should be RARE since we validate everything upfront in the tool
            error_msg = str(e)

            # Worker-related errors (race conditions)
            if "Worker" in error_msg and "not found" in error_msg:
                raise ValueError(f"{E_WRK_001}: Worker '{request.worker_id}' not found") from e
            elif "worker" in error_msg.lower() and "active" in error_msg.lower():
                raise ValueError(f"{E_WRK_002}: {error_msg}") from e

            # Position-related errors (from race conditions)
            elif "Position" in error_msg and "not found" in error_msg:
                raise ValueError(f"{E_POS_001}: {error_msg}") from e
            elif "Position" in error_msg and ("not open" in error_msg or "filled" in error_msg):
                raise ValueError(f"{E_POS_002}: {error_msg}") from e

            # Validation error (no fields provided) or unknown - re-raise as-is
            raise
