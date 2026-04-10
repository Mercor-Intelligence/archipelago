"""Exception handling tools for Workday HCM pre-onboarding."""

from db.repositories.exception_repository import ExceptionRepository
from db.session import get_session
from mcp_auth import require_roles
from models import ApproveExceptionInput, ExceptionOutput, RequestExceptionInput
from utils.decorators import make_async_background


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin", "hr_business_partner")
def workday_exception_request(
    request: RequestExceptionInput,
) -> ExceptionOutput:
    """Request an exception for a milestone that cannot be completed normally."""
    with get_session() as session:
        repo = ExceptionRepository()
        result = repo.create_exception(session, request)
        return result


@make_async_background
@require_roles("hr_admin")
def workday_exception_approve(
    request: ApproveExceptionInput,
) -> ExceptionOutput:
    """Approve or deny an exception request (HR Admin only)."""
    # Validate actor_persona before database operations
    if request.actor_persona != "hr_admin":
        raise ValueError("E_AUTH_001: Only hr_admin can approve exceptions")

    with get_session() as session:
        repo = ExceptionRepository()

        # Check if exception exists first
        existing = repo.get_by_id(session, request.exception_id)
        if not existing:
            raise ValueError(f"E_EXC_001: Exception {request.exception_id} not found")

        # Check if already processed
        if existing.approval_status != "pending":
            raise ValueError(
                f"E_EXC_002: Exception already processed (status: {existing.approval_status})"
            )

        result = repo.approve_exception(session, request)
        return result
