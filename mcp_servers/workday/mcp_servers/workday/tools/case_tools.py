"""Case management tools for Workday HCM pre-onboarding coordination."""

from db.models import Case
from db.repositories.case_repository import CaseRepository
from db.session import get_session
from loguru import logger
from mcp_auth import require_roles, require_scopes
from models import (
    VALID_COUNTRY_CODES,
    VALID_PERSONAS,
    AssignOwnerInput,
    CaseDetailOutput,
    CaseOutput,
    CaseSnapshotInput,
    CaseSnapshotOutput,
    CreateCaseInput,
    GetCaseInput,
    SearchCasesInput,
    SearchCasesOutput,
    UpdateCaseStatusInput,
)
from sqlalchemy import select
from utils.decorators import make_async_background

# Error code constants (per BUILD_PLAN_v2.md Section 2.5)
E_CASE_001 = "E_CASE_001"  # Case not found
E_CASE_002 = "E_CASE_002"  # Case already exists
E_CASE_003 = "E_CASE_003"  # Invalid case status transition
E_AUTH_001 = "E_AUTH_001"  # Invalid persona

# Valid status transitions (from -> [allowed to states])
VALID_STATUS_TRANSITIONS = {
    "open": ["in_progress"],
    "in_progress": ["pending_approval", "resolved"],
    "pending_approval": ["in_progress", "resolved"],
    "resolved": ["closed"],
    "closed": [],  # Terminal state
}


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_create_case(request: CreateCaseInput) -> CaseOutput:
    """Create a new pre-onboarding case with candidate context and milestones."""
    logger.info(f"Creating case: {request.case_id} for candidate: {request.candidate_id}")

    # Validate owner_persona
    if request.owner_persona not in VALID_PERSONAS:
        logger.warning(f"Invalid owner_persona: {request.owner_persona}")
        raise ValueError(f"{E_AUTH_001}: Invalid persona '{request.owner_persona}'")

    if request.owner_persona not in ["pre_onboarding_coordinator", "hr_admin"]:
        logger.warning(f"Persona {request.owner_persona} not authorized to create cases")
        raise ValueError(f"{E_AUTH_001}: Persona not authorized to create cases")

    # Validate country code
    if request.country.upper() not in VALID_COUNTRY_CODES:
        logger.warning(f"Invalid country code: {request.country}")
        raise ValueError(f"Invalid country code: {request.country}")

    repository = CaseRepository()

    with get_session() as session:
        # Check if case already exists
        existing_case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if existing_case:
            logger.warning(f"Case already exists: {request.case_id}")
            raise ValueError(f"{E_CASE_002}: Case already exists: {request.case_id}")

        # Create the case with milestones and audit entry
        result = repository.create(session, request)

        logger.info(
            f"Successfully created case: {result.case_id} with {len(result.milestones)} milestones"
        )

        return result


@make_async_background
@require_scopes("read")
def workday_get_case(request: GetCaseInput) -> CaseDetailOutput:
    """Retrieve a pre-onboarding case with optional tasks and audit trail."""
    logger.info(f"Retrieving case: {request.case_id}")

    repository = CaseRepository()

    with get_session() as session:
        result = repository.get_by_id(session, request)

        if not result:
            logger.warning(f"Case not found: {request.case_id}")
            raise ValueError(f"{E_CASE_001}: Case not found")

        logger.info(
            f"Successfully retrieved case: {request.case_id} "
            f"(tasks={'included' if request.include_tasks else 'excluded'}, "
            f"audit={'included' if request.include_audit else 'excluded'})"
        )

        return result


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_update_case(request: UpdateCaseStatusInput) -> CaseOutput:
    """Transition case status with validation and audit logging."""
    logger.info(
        f"Updating case status: {request.case_id} -> {request.new_status} "
        f"(actor: {request.actor_persona})"
    )

    # Validate actor_persona
    if request.actor_persona not in VALID_PERSONAS:
        logger.warning(f"Invalid actor_persona: {request.actor_persona}")
        raise ValueError(f"{E_AUTH_001}: Invalid Persona")

    # Validate persona has permission for this transition
    if request.actor_persona not in ["pre_onboarding_coordinator", "hr_admin"]:
        logger.warning(f"Persona {request.actor_persona} not authorized to update case status")
        raise ValueError(f"{E_AUTH_001}: Persona not authorized")

    repository = CaseRepository()

    with get_session() as session:
        # Perform the update with atomic validation under lock
        # The repository validates the transition while holding a row lock
        # to prevent race conditions
        try:
            result = repository.update_status(session, request, VALID_STATUS_TRANSITIONS)
        except ValueError as e:
            error_msg = str(e)
            if "not found" in error_msg:
                logger.warning(f"Case not found: {request.case_id}")
                raise ValueError(f"{E_CASE_001}: Case not found")
            elif "Invalid status transition" in error_msg:
                logger.warning(f"Invalid status transition: {error_msg}")
                raise ValueError(f"{E_CASE_003}: {error_msg}")
            raise

        logger.info(f"Successfully updated case status: {request.case_id} -> {request.new_status}")

        return result


@make_async_background
@require_roles("pre_onboarding_coordinator", "hr_admin")
def workday_assign_owner_case(request: AssignOwnerInput) -> CaseOutput:
    """Assign or reassign the case owner with audit logging."""
    logger.info(
        f"Assigning case owner: {request.case_id} -> {request.new_owner_persona} "
        f"(actor: {request.actor_persona})"
    )

    # Validate actor_persona
    if request.actor_persona not in VALID_PERSONAS:
        logger.warning(f"Invalid actor_persona: {request.actor_persona}")
        raise ValueError(f"{E_AUTH_001}: Invalid Persona")

    # Validate new_owner_persona
    if request.new_owner_persona not in VALID_PERSONAS:
        logger.warning(f"Invalid new_owner_persona: {request.new_owner_persona}")
        raise ValueError(f"{E_AUTH_001}: Invalid persona '{request.new_owner_persona}'")

    # Unauthorized for anyone other than coordinator and hr_admin as per Persona Access Matrix.
    if request.actor_persona not in ["pre_onboarding_coordinator", "hr_admin"]:
        logger.warning(f"Persona {request.actor_persona} not authorized to reassign cases")
        raise ValueError(f"{E_AUTH_001}: Persona not authorized")

    repository = CaseRepository()

    with get_session() as session:
        # Check if case exists
        case = session.execute(
            select(Case).where(Case.case_id == request.case_id)
        ).scalar_one_or_none()

        if not case:
            logger.warning(f"Case not found: {request.case_id}")
            raise ValueError(f"{E_CASE_001}: Case not found")

        old_owner = case.owner_persona

        # Perform the update
        result = repository.assign_owner(session, request)

        logger.info(
            f"Successfully reassigned case owner: {request.case_id} "
            f"{old_owner} -> {request.new_owner_persona}"
        )

        return result


@make_async_background
@require_scopes("read")
def workday_search_case(request: SearchCasesInput) -> SearchCasesOutput:
    """Search cases by status, owner, role, country, or date range with pagination."""
    logger.info(
        f"Searching cases: status={request.status}, owner={request.owner_persona}, "
        f"country={request.country}, role={request.role}"
    )

    repository = CaseRepository()

    with get_session() as session:
        result = repository.search(session, request)

        logger.info(
            f"Search completed: found {result.total_count} cases, "
            f"returning page {result.page_number} ({len(result.cases)} results)"
        )

        return result


@make_async_background
@require_scopes("read")
def workday_snapshot_case(request: CaseSnapshotInput) -> CaseSnapshotOutput:
    """Retrieve a complete point-in-time snapshot of a case with all related data."""
    logger.info(f"Generating snapshot for case: {request.case_id}")

    if request.as_of_date:
        logger.info(f"as_of_date specified: {request.as_of_date} (not yet implemented)")

    repository = CaseRepository()

    with get_session() as session:
        result = repository.get_case_snapshot(session, request)

        if not result:
            logger.warning(f"Case not found: {request.case_id}")
            raise ValueError(f"{E_CASE_001}: Case not found")

        logger.info(
            f"Successfully generated snapshot for case: {request.case_id} "
            f"(policies: {len(result.policy_references)}, "
            f"hcm_state: {result.hcm_state is not None}, "
            f"write_history: {len(result.hcm_write_history)})"
        )

        return result
