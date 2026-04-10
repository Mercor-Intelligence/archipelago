"""Application tools for the Greenhouse MCP server."""

from auth.permissions import Permission as Perm
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from mcp_auth import require_scopes
from schemas import (
    AdvanceApplicationInput,
    ApplicationOutput,
    CreateApplicationInput,
    GetApplicationInput,
    HireApplicationInput,
    ListApplicationsInput,
    ListApplicationsOutput,
    PaginationMeta,
    RejectApplicationInput,
)
from services.clean_provider import (
    ApplicationAlreadyHiredError,
    ApplicationAlreadyRejectedError,
    ApplicationIsProspectError,
    ApplicationNotFoundError,
    ApplicationRejectedError,
    CandidateNotFoundError,
    CleanProvider,
    DuplicateApplicationError,
    InvalidJobOpeningError,
    InvalidRejectionReasonError,
    InvalidStageError,
    InvalidStageTransitionError,
    JobNotFoundError,
    JobNotOpenError,
    SourceNotFoundError,
    StageMismatchError,
    UserNotFoundError,
)
from services.pagination import build_pagination_links


@require_scopes(Perm.APPLICATION_READ.value)
async def greenhouse_applications_get(params: GetApplicationInput) -> ApplicationOutput:
    """Retrieve a single application by ID."""
    provider = CleanProvider()
    try:
        return await provider.get_application(params.application_id)
    except ApplicationNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@require_scopes(Perm.APPLICATION_READ.value)
async def greenhouse_applications_list(params: ListApplicationsInput) -> ListApplicationsOutput:
    """List applications with filters and pagination."""

    provider = CleanProvider()
    filters = {
        "job_id": params.job_id,
        "status": params.status,
        "candidate_id": params.candidate_id,
        "current_stage_id": params.current_stage_id,
        "created_before": params.created_before,
        "created_after": params.created_after,
        "last_activity_after": params.last_activity_after,
    }

    applications = await provider.list_applications(
        page=params.page,
        per_page=params.per_page,
        **filters,
    )
    total = None
    if not params.skip_count:
        total = await provider.count_applications(**filters)

    links = build_pagination_links("/applications", params.page, params.per_page, total)
    meta = PaginationMeta(
        per_page=params.per_page,
        page=params.page,
        total=total,
        links=links,
    )

    return ListApplicationsOutput(applications=applications, meta=meta)


@require_scopes(Perm.APPLICATION_ADVANCE.value)
async def greenhouse_applications_advance_stage(
    params: AdvanceApplicationInput,
) -> ApplicationOutput:
    """Advance an application to the next or specified pipeline stage."""
    provider = CleanProvider()
    try:
        return await provider.advance_application(
            params.application_id,
            from_stage_id=params.from_stage_id,
            to_stage_id=params.to_stage_id,
        )
    except ApplicationNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except (InvalidStageTransitionError, StageMismatchError) as exc:
        raise ToolError(str(exc)) from exc


@require_scopes(Perm.APPLICATION_HIRE.value)
async def greenhouse_applications_hire(params: HireApplicationInput) -> ApplicationOutput:
    """Mark an application as hired."""
    provider = CleanProvider()
    try:
        return await provider.hire_application(
            application_id=params.application_id,
            opening_id=params.opening_id,
            start_date=params.start_date,
            close_reason_id=params.close_reason_id,
        )
    except ApplicationNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except (
        ApplicationAlreadyHiredError,
        ApplicationRejectedError,
        ApplicationIsProspectError,
        InvalidJobOpeningError,
    ) as exc:
        raise ToolError(str(exc)) from exc


@require_scopes(Perm.APPLICATION_REJECT.value)
async def greenhouse_applications_reject(params: RejectApplicationInput) -> ApplicationOutput:
    """Reject an application with optional rejection reason and notes."""
    provider = CleanProvider()
    try:
        return await provider.reject_application(
            application_id=params.application_id,
            rejection_reason_id=params.rejection_reason_id,
            notes=params.notes,
        )
    except ApplicationNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except (
        ApplicationAlreadyRejectedError,
        ApplicationAlreadyHiredError,
        InvalidRejectionReasonError,
    ) as exc:
        raise ToolError(str(exc)) from exc


@require_scopes(Perm.APPLICATION_CREATE.value)
async def greenhouse_applications_create(params: CreateApplicationInput) -> ApplicationOutput:
    """Create an application for a candidate to a job."""
    provider = CleanProvider()
    try:
        # Convert AnswerInput models to dicts for the provider
        answers = None
        if params.answers:
            answers = [{"question": a.question, "answer": a.answer} for a in params.answers]

        # Convert ReferrerInput model to dict for the provider
        referrer = None
        if params.referrer:
            referrer = params.referrer.model_dump()

        # Convert AttachmentInput models to dicts for the provider
        attachments = None
        if params.attachments:
            attachments = [att.model_dump() for att in params.attachments]

        return await provider.create_application(
            candidate_id=params.candidate_id,
            job_id=params.job_id,
            source_id=params.source_id,
            initial_stage_id=params.initial_stage_id,
            recruiter_id=params.recruiter_id,
            coordinator_id=params.coordinator_id,
            referrer=referrer,
            attachments=attachments,
            answers=answers,
        )
    except (CandidateNotFoundError, JobNotFoundError, SourceNotFoundError) as exc:
        raise NotFoundError(str(exc)) from exc
    except (JobNotOpenError, DuplicateApplicationError, InvalidStageError) as exc:
        raise ToolError(str(exc)) from exc
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


def register_application_tools(mcp: FastMCP) -> None:
    """Register the application tools with the MCP server."""
    mcp.tool()(greenhouse_applications_list)
    mcp.tool()(greenhouse_applications_get)
    mcp.tool()(greenhouse_applications_advance_stage)
    mcp.tool()(greenhouse_applications_create)
    mcp.tool()(greenhouse_applications_hire)
    mcp.tool()(greenhouse_applications_reject)
