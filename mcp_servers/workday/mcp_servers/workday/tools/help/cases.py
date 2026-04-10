"""Case MCP tools for Workday Help."""

from loguru import logger
from mcp_auth import get_current_user
from schemas.help.case_schemas import (
    CaseSummary,
    CreateCaseRequest,
    CreateCaseResponse,
    GetCaseRequest,
    GetCaseResponse,
    ReassignCaseOwnerRequest,
    ReassignCaseOwnerResponse,
    SearchCasesRequest,
    SearchCasesResponse,
    UpdateCaseDueDateRequest,
    UpdateCaseDueDateResponse,
    UpdateCaseStatusRequest,
    UpdateCaseStatusResponse,
)
from services.case_service import CaseService
from utils.decorators import make_async_background
from validators.business_rules import SUPPORTED_PERSONAS

_case_service = CaseService()


def _resolve_actor(
    actor: str | None,
    *,
    fallback: str | None = None,
    require: bool = True,
) -> str | None:
    """Return an actor email when required, otherwise allow None."""
    if actor:
        return actor
    if fallback:
        return fallback
    if require:
        raise ValueError("E_AUTH_002: actor email is required for case operations")
    return None


def _derive_context(
    actor_persona: str | None,
    actor: str | None,
    *,
    default_persona: str = "case_owner",
    fallback_actor: str | None = None,
) -> tuple[str, str | None]:
    """Resolve persona/actor using request values with auth context fallback.

    If actor_persona is not provided, searches user's roles for a compatible
    persona from SUPPORTED_PERSONAS (case_owner, hr_admin, manager, hr_analyst).
    Falls back to default_persona if no compatible role is found.
    """
    user = get_current_user()

    # Try to find a compatible persona from user's roles
    persona = actor_persona
    if not persona:
        user_roles = user.get("roles") or []
        for role in user_roles:
            if role in SUPPORTED_PERSONAS:
                persona = role
                break
        if not persona:
            persona = default_persona

    resolved_actor = actor or user.get("username") or user.get("userId") or fallback_actor
    return persona, resolved_actor


@make_async_background
def workday_help_cases_create(
    request: CreateCaseRequest,
) -> CreateCaseResponse:
    """Create a case with validation, timeline, and audit logging."""
    actor_persona, actor = _derive_context(
        request.actor_persona,
        request.actor,
        fallback_actor=request.owner,
    )
    logger.info(
        f"Creating case: candidate={request.candidate_identifier}, "
        f"owner={request.owner}, persona={actor_persona}"
    )
    actor_email = _resolve_actor(actor, fallback=request.owner)

    try:
        result = _case_service.create_case(
            case_type=request.case_type,
            owner=request.owner,
            status=request.status,
            candidate_identifier=request.candidate_identifier,
            due_date=request.due_date,
            metadata=request.metadata,
            requested_case_id=request.case_id,
            actor=actor_email,
            actor_persona=actor_persona,
        )

        case = result["case"]
        return CreateCaseResponse(
            case_id=case["case_id"],
            case_type=case["case_type"],
            owner=case["owner"],
            status=case["status"],
            candidate_identifier=case["candidate_identifier"],
            due_date=case["due_date"],
            created_at=case["created_at"],
            updated_at=case["updated_at"],
            metadata=case["metadata"],
            timeline_event_id=result["timeline_event_id"],
            audit_log_id=result["audit_log_id"],
        )
    except ValueError:
        raise
    except Exception as err:
        logger.error(f"Unexpected error creating case: {err}")
        raise ValueError(f"E_GEN_001: Failed to create case: {err}") from err


@make_async_background
def workday_help_cases_get(
    request: GetCaseRequest,
) -> GetCaseResponse:
    """Retrieve a case by ID."""
    actor_persona, actor = _derive_context(request.actor_persona, request.actor)
    logger.info(f"Retrieving case: case_id={request.case_id}, persona={actor_persona}")

    try:
        case = _case_service.get_case(
            case_id=request.case_id,
            actor_persona=actor_persona,
            actor=_resolve_actor(actor, require=False),
        )
        return GetCaseResponse(
            case_id=case["case_id"],
            case_type=case["case_type"],
            owner=case["owner"],
            status=case["status"],
            candidate_identifier=case["candidate_identifier"],
            due_date=case["due_date"],
            created_at=case["created_at"],
            updated_at=case["updated_at"],
            metadata=case["metadata"],
        )
    except ValueError:
        raise
    except Exception as err:
        logger.error(f"Unexpected error retrieving case: {err}")
        raise ValueError(f"E_GEN_001: Failed to retrieve case: {err}") from err


@make_async_background
def workday_help_cases_update_status(
    request: UpdateCaseStatusRequest,
) -> UpdateCaseStatusResponse:
    """Update case status through the allowed state machine."""
    actor_persona, actor = _derive_context(request.actor_persona, request.actor)
    logger.info(
        f"Updating status: case_id={request.case_id}, "
        f"new_status={request.new_status}, "
        f"persona={actor_persona}"
    )

    try:
        result = _case_service.update_status(
            case_id=request.case_id,
            current_status=request.current_status,
            new_status=request.new_status,
            rationale=request.rationale,
            actor=_resolve_actor(actor),
            actor_persona=actor_persona,
        )

        return UpdateCaseStatusResponse(
            case_id=result["case_id"],
            previous_status=result["previous_status"],
            new_status=result["new_status"],
            updated_at=result["updated_at"],
            timeline_event_id=result["timeline_event_id"],
            audit_log_id=result["audit_log_id"],
        )
    except ValueError:
        raise
    except Exception as err:
        logger.error(f"Unexpected error updating case status: {err}")
        raise ValueError(f"E_GEN_001: Failed to update case status: {err}") from err


@make_async_background
def workday_help_cases_reassign_owner(
    request: ReassignCaseOwnerRequest,
) -> ReassignCaseOwnerResponse:
    """Reassign case ownership with rationale tracking."""
    actor_persona, actor = _derive_context(request.actor_persona, request.actor)
    logger.info(
        f"Reassigning owner: case_id={request.case_id}, "
        f"new_owner={request.new_owner}, "
        f"persona={actor_persona}"
    )

    try:
        result = _case_service.reassign_owner(
            case_id=request.case_id,
            new_owner=request.new_owner,
            rationale=request.rationale,
            actor=_resolve_actor(actor),
            actor_persona=actor_persona,
        )

        return ReassignCaseOwnerResponse(
            case_id=result["case_id"],
            previous_owner=result["previous_owner"],
            new_owner=result["new_owner"],
            updated_at=result["updated_at"],
            timeline_event_id=result["timeline_event_id"],
            audit_log_id=result["audit_log_id"],
        )
    except ValueError:
        raise
    except Exception as err:
        logger.error(f"Unexpected error reassigning owner: {err}")
        raise ValueError(f"E_GEN_001: Failed to reassign owner: {err}") from err


@make_async_background
def workday_help_cases_update_due_date(
    request: UpdateCaseDueDateRequest,
) -> UpdateCaseDueDateResponse:
    """Update case due date with required rationale."""
    actor_persona, actor = _derive_context(request.actor_persona, request.actor)
    logger.info(
        f"Updating due date: case_id={request.case_id}, "
        f"persona={actor_persona}, "
        f"new_due_date={request.new_due_date}"
    )

    try:
        result = _case_service.update_due_date(
            case_id=request.case_id,
            new_due_date=request.new_due_date,
            rationale=request.rationale,
            actor=_resolve_actor(actor),
            actor_persona=actor_persona,
        )

        return UpdateCaseDueDateResponse(
            case_id=result["case_id"],
            previous_due_date=result["previous_due_date"],
            new_due_date=result["new_due_date"],
            updated_at=result["updated_at"],
            timeline_event_id=result["timeline_event_id"],
            audit_log_id=result["audit_log_id"],
        )
    except ValueError:
        raise
    except Exception as err:
        logger.error(f"Unexpected error updating due date: {err}")
        raise ValueError(f"E_GEN_001: Failed to update due date: {err}") from err


@make_async_background
def workday_help_cases_search(
    request: SearchCasesRequest,
) -> SearchCasesResponse:
    """Search cases with filters, multi-status support, and pagination."""
    actor_persona, actor = _derive_context(request.actor_persona, request.actor)
    logger.info(
        f"Searching cases: statuses={request.status}, "
        f"owner={request.owner}, "
        f"persona={actor_persona}"
    )

    try:
        filters = request.model_dump(
            include={"status", "owner", "candidate_identifier", "created_after", "created_before"}
        )
        result = _case_service.search_cases(
            statuses=filters.get("status"),
            owner=filters.get("owner"),
            candidate_identifier=filters.get("candidate_identifier"),
            created_after=filters.get("created_after"),
            created_before=filters.get("created_before"),
            cursor=request.cursor,
            limit=request.limit,
            actor_persona=actor_persona,
            actor=_resolve_actor(actor, require=False),
        )

        summaries = [
            CaseSummary(
                case_id=case["case_id"],
                case_type=case["case_type"],
                owner=case["owner"],
                status=case["status"],
                candidate_identifier=case["candidate_identifier"],
                due_date=case["due_date"],
                created_at=case["created_at"],
                updated_at=case["updated_at"],
            )
            for case in result["cases"]
        ]

        return SearchCasesResponse(
            cases=summaries,
            next_cursor=result["next_cursor"],
            has_more=result["has_more"],
            limit=result["limit"],
        )
    except ValueError:
        raise
    except Exception as err:
        logger.error(f"Unexpected error searching cases: {err}")
        raise ValueError(f"E_GEN_001: Failed to search cases: {err}") from err
