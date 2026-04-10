"""Milestone tools for Workday HCM pre-onboarding coordination."""

from db.models import Case, CaseException, Milestone
from db.repositories.case_repository import CaseRepository
from db.session import get_session
from mcp_auth import require_roles, require_scopes
from models import (
    ListMilestonesInput,
    MilestoneListOutput,
    MilestoneOutput,
    UpdateMilestoneInput,
)
from sqlalchemy import select
from utils.decorators import make_async_background

from tools.constants import E_CASE_001, E_MILE_001, E_MILE_002, E_MILE_003

# Valid status transitions (from -> [allowed to states])
VALID_STATUS_TRANSITIONS = {
    "pending": ["in_progress", "completed", "blocked"],
    "in_progress": ["completed", "blocked"],
    "blocked": ["in_progress", "waived"],  # waived requires approved exception
    "completed": [],  # Terminal state
    "waived": [],  # Terminal state
}


@make_async_background
@require_scopes("read")
def workday_milestones_list(request: ListMilestonesInput) -> MilestoneListOutput:
    """List all milestones for a pre-onboarding case."""
    repository = CaseRepository()

    with get_session() as session:
        # Validate case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")

        # Get milestones via repository
        return repository.list_milestones(session, request)


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_milestones_update(request: UpdateMilestoneInput) -> MilestoneOutput:
    """Update a milestone's status with optional evidence."""
    repository = CaseRepository()

    with get_session() as session:
        # 1. Validate case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")

        # 2. Validate milestone exists and check transition validity
        milestone = session.execute(
            select(Milestone).where(
                Milestone.case_id == request.case_id,
                Milestone.milestone_type == request.milestone_type,
            )
        ).scalar_one_or_none()

        if not milestone:
            raise ValueError(
                f"{E_MILE_001}: Milestone '{request.milestone_type}' "
                f"not found for case '{request.case_id}'"
            )

        # 3. Validate transition is allowed before any gating checks
        current_status = milestone.status
        allowed_transitions = VALID_STATUS_TRANSITIONS.get(current_status, [])
        if request.new_status not in allowed_transitions:
            if not allowed_transitions:
                raise ValueError(
                    f"{E_MILE_002}: Invalid status transition from '{current_status}' "
                    f"to '{request.new_status}'. Allowed transitions: none (terminal state)"
                )
            raise ValueError(
                f"{E_MILE_002}: Invalid status transition from '{current_status}' "
                f"to '{request.new_status}'. Allowed transitions: {allowed_transitions}"
            )

        # 4. Gating check: approved exception required for waived transition
        # Only checked after confirming the transition is valid (blocked → waived)
        if request.new_status == "waived":
            # Use .first() instead of .scalar_one_or_none() to handle multiple
            # approved exceptions for the same milestone (any one is sufficient)
            approved_exception = (
                session.execute(
                    select(CaseException).where(
                        CaseException.case_id == request.case_id,
                        CaseException.milestone_type == request.milestone_type,
                        CaseException.approval_status == "approved",
                    )
                )
                .scalars()
                .first()
            )

            if not approved_exception:
                raise ValueError(
                    f"{E_MILE_003}: Approved exception required to waive "
                    f"milestone '{request.milestone_type}'. Request an exception first."
                )

        # 6. Update milestone via repository with atomic validation under lock
        # The repository validates the transition while holding a row lock
        # to prevent race conditions
        try:
            return repository.update_milestone(session, request, VALID_STATUS_TRANSITIONS)
        except ValueError as e:
            error_msg = str(e)
            if "not found" in error_msg:
                raise ValueError(
                    f"{E_MILE_001}: Milestone '{request.milestone_type}' "
                    f"not found for case '{request.case_id}'"
                )
            elif "Invalid status transition" in error_msg:
                raise ValueError(f"{E_MILE_002}: {error_msg}")
            raise
