"""Job tool wiring for the Greenhouse MCP server."""

from auth.permissions import Permission as Perm
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from mcp_auth import require_scopes
from schemas import GetJobStagesInput, JobOutput, JobStageOutput, PaginationMeta
from schemas.jobs import (
    CreateJobInput,
    GetJobInput,
    ListJobsInput,
    ListJobsOutput,
    UpdateJobInput,
)
from services.clean_provider import (
    CleanProvider,
    InvalidDepartmentError,
    InvalidOfficeError,
    JobNotFoundError,
)
from services.pagination import build_pagination_links


@require_scopes(Perm.JOB_READ.value)
async def greenhouse_jobs_list(params: ListJobsInput) -> ListJobsOutput:
    """List jobs with optional filters and pagination."""
    provider = CleanProvider()
    jobs = await provider.list_jobs(
        page=params.page,
        per_page=params.per_page,
        status=params.status,
        department_id=params.department_id,
        office_id=params.office_id,
        requisition_id=params.requisition_id,
        created_before=params.created_before,
        created_after=params.created_after,
        updated_before=params.updated_before,
        updated_after=params.updated_after,
    )
    total = None
    if not params.skip_count:
        total = await provider.count_jobs(
            status=params.status,
            department_id=params.department_id,
            office_id=params.office_id,
            requisition_id=params.requisition_id,
            created_before=params.created_before,
            created_after=params.created_after,
            updated_before=params.updated_before,
            updated_after=params.updated_after,
        )
    links = build_pagination_links("/jobs", params.page, params.per_page, total)
    meta = PaginationMeta(per_page=params.per_page, page=params.page, total=total, links=links)
    return ListJobsOutput(jobs=jobs, meta=meta)


@require_scopes(Perm.JOB_READ.value)
async def greenhouse_jobs_get(params: GetJobInput) -> JobOutput:
    """Retrieve a single job by ID."""
    provider = CleanProvider()
    try:
        result = await provider.get_job(params.job_id)
        return JobOutput.model_validate(result)
    except JobNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@require_scopes(Perm.JOB_READ.value)
async def greenhouse_jobs_get_stages(params: GetJobStagesInput) -> list[JobStageOutput]:
    """Retrieve all pipeline stages for a job's hiring process."""
    provider = CleanProvider()
    try:
        return await provider.get_job_stages(
            params.job_id,
            created_before=params.created_before,
            created_after=params.created_after,
            updated_before=params.updated_before,
            updated_after=params.updated_after,
        )
    except JobNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@require_scopes(Perm.JOB_CREATE.value)
async def greenhouse_jobs_create(params: CreateJobInput) -> JobOutput:
    """Create a new job with default pipeline stages."""
    provider = CleanProvider()
    try:
        result = await provider.create_job(
            name=params.name,
            template_job_id=params.template_job_id,
            requisition_id=params.requisition_id,
            notes=params.notes,
            anywhere=params.anywhere,
            department_id=params.department_id,
            office_ids=params.office_ids,
            opening_ids=params.opening_ids,
            number_of_openings=params.number_of_openings,
            status=params.status,
        )
        return JobOutput.model_validate(result)
    except (InvalidDepartmentError, InvalidOfficeError) as exc:
        raise ToolError(str(exc)) from exc


@require_scopes(Perm.JOB_UPDATE.value)
async def greenhouse_jobs_update(params: UpdateJobInput) -> JobOutput:
    """Update an existing job with PATCH semantics."""
    provider = CleanProvider()
    try:
        result = await provider.update_job(
            params.job_id,
            name=params.name,
            requisition_id=params.requisition_id,
            notes=params.notes,
            status=params.status,
            department_id=params.department_id,
            office_ids=params.office_ids,
        )
        return JobOutput.model_validate(result)
    except JobNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except (InvalidDepartmentError, InvalidOfficeError) as exc:
        raise ToolError(str(exc)) from exc


def register_job_tools(mcp: FastMCP) -> None:
    """Register the job list/get/stages/create/update tools with the MCP server."""
    mcp.tool()(greenhouse_jobs_list)
    mcp.tool()(greenhouse_jobs_get)
    mcp.tool()(greenhouse_jobs_get_stages)
    mcp.tool()(greenhouse_jobs_create)
    mcp.tool()(greenhouse_jobs_update)
