from db.models import Case
from db.repositories.audit_repository import AuditRepository
from db.session import get_session
from mcp_auth import require_scopes
from models import AuditHistoryOutput, GetAuditHistoryInput
from sqlalchemy import select
from utils.decorators import make_async_background

# Error code constants
E_CASE_001 = "E_CASE_001"  # Case not found


@make_async_background
@require_scopes("read")
def workday_audit_get_history(request: GetAuditHistoryInput) -> AuditHistoryOutput:
    """Retrieve audit history for a pre-onboarding case with optional filters."""
    repository = AuditRepository()

    with get_session() as session:
        # Validate case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            raise ValueError(f"{E_CASE_001}: Case '{request.case_id}' not found")

        # Get audit history
        return repository.get_history(session, request)
