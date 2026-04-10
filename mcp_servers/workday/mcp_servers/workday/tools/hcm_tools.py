"""HCM tools for Workday pre-onboarding coordination."""

from db.repositories.hcm_repository import HCMRepository
from db.session import get_session
from mcp_auth import require_roles, require_scopes
from models import (
    ConfirmStartDateInput,
    ConfirmStartDateOutput,
    HCMContextOutput,
    PositionContextOutput,
    ReadHCMContextInput,
    ReadPositionInput,
    UpdateReadinessInput,
    UpdateReadinessOutput,
)
from utils.decorators import make_async_background

# Error code constants (per BUILD_PLAN_v2.md § 2.5)
E_CASE_001 = "E_CASE_001"  # Case not found


# =============================================================================
# Read Tools (No gating required)
# =============================================================================


@make_async_background
@require_scopes("read")
def workday_hcm_read_context(request: ReadHCMContextInput) -> HCMContextOutput:
    """Retrieve HCM context for a case including onboarding status and start dates."""
    repository = HCMRepository()

    with get_session() as session:
        # First check if case exists
        from db.models import Case
        from sqlalchemy import select

        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")

        # Get HCM context (may be None if no HCM updates yet)
        context = repository.get_hcm_context(session, request.case_id)

        if context is None:
            # Return empty context for case with no HCM updates yet
            return HCMContextOutput(
                case_id=request.case_id,
                worker_id=None,
                onboarding_status=None,
                onboarding_readiness=False,
                proposed_start_date=case.proposed_start_date,
                confirmed_start_date=None,
                hire_finalized=False,
                last_updated=None,
            )

        return context


@make_async_background
@require_scopes("read")
def workday_hcm_read_position(request: ReadPositionInput) -> PositionContextOutput:
    """Retrieve position context with policy-derived requirements for a case."""
    repository = HCMRepository()

    with get_session() as session:
        # Get position context (validates case exists internally)
        context = repository.get_position_context(session, request.case_id)

        if context is None:
            raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")

        return context


# =============================================================================
# Write-Back Tools (Gated operations)
# =============================================================================


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_hcm_confirm_start_date(
    request: ConfirmStartDateInput,
) -> ConfirmStartDateOutput:
    """Confirm a start date for a case with gating validation."""
    repository = HCMRepository()
    with get_session() as session:
        return repository.confirm_start_date(session, request)


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_hcm_update_readiness(
    request: UpdateReadinessInput,
) -> UpdateReadinessOutput:
    """Update onboarding readiness flag for a case."""
    repository = HCMRepository()
    with get_session() as session:
        return repository.update_readiness(session, request)
