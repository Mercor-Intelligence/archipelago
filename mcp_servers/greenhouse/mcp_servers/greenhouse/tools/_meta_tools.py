"""Meta-tools for Greenhouse MCP Server (LLM Interface).

This module provides consolidated domain-based tools with action parameter routing.
These meta-tools reduce token usage for LLMs by ~80% compared to individual tools.

Meta-tools delegate to individual tool functions with proper permission enforcement:
1. Each meta-tool uses @action_scopes decorator to enforce permissions per-action
2. The decorator reads _required_scopes from individual tool functions
3. AuthGuard sees the meta-tool as "permissioned" (has _action_permissions marker)
4. Actual scope checking happens at runtime based on the action parameter

File naming: Underscore prefix (_meta_tools.py) hides from UI scanner,
so the UI shows individual tools while LLMs use these consolidated meta-tools.

Usage in main.py:
    from tools._meta_tools import register_meta_tools
    register_meta_tools(mcp)
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, Literal, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp_auth import get_current_user, public_tool
from mcp_auth.errors import AuthorizationError
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field
from schemas import (
    ActivityFeedOutput,
    AddCandidateNoteInput,
    AddCandidateTagInput,
    AddCandidateTagOutput,
    ApplicationOutput,
    CandidateNoteOutput,
    CandidateOutput,
    CandidateSearchResultOutput,
    CreateCandidateInput,
    GetCandidateInput,
    JobBoardApplyInput,
    JobBoardApplyOutput,
    JobOutput,
    JobStageOutput,
    ListJobBoardJobsInput,
    PaginationMeta,
    ScorecardOutput,
    SearchCandidatesInput,
    UpdateCandidateInput,
    UserOutput,
)
from schemas.activity import GetActivityFeedInput
from schemas.admin import GreenhouseResetStateInput, GreenhouseResetStateResponse
from schemas.applications import (
    AdvanceApplicationInput,
    AnswerInput,
    AttachmentInput,
    CreateApplicationInput,
    GetApplicationInput,
    HireApplicationInput,
    ListApplicationsInput,
    ReferrerInput,
    RejectApplicationInput,
)
from schemas.jobs import (
    CreateJobInput,
    GetJobInput,
    GetJobStagesInput,
    ListJobsInput,
    UpdateJobInput,
)
from schemas.scorecards import ListFeedbackInput, SubmitFeedbackInput
from schemas.users import CreateUserInput, GetUserInput, ListUsersInput

# Import individual tool functions - meta-tools delegate to these
from tools.activity import greenhouse_activity_get
from tools.admin import (
    ExportSnapshotInput,
    ExportSnapshotOutput,
    greenhouse_export_snapshot,
    greenhouse_reset_state,
)
from tools.applications import (
    greenhouse_applications_advance_stage,
    greenhouse_applications_create,
    greenhouse_applications_get,
    greenhouse_applications_hire,
    greenhouse_applications_list,
    greenhouse_applications_reject,
)
from tools.candidates import (
    greenhouse_candidates_add_note,
    greenhouse_candidates_add_tag,
    greenhouse_candidates_create,
    greenhouse_candidates_get,
    greenhouse_candidates_search,
    greenhouse_candidates_update,
)
from tools.feedback import (
    greenhouse_feedback_list,
    greenhouse_feedback_submit,
)
from tools.jobboard import greenhouse_jobboard_apply, greenhouse_jobboard_list_jobs
from tools.jobs import (
    greenhouse_jobs_create,
    greenhouse_jobs_get,
    greenhouse_jobs_get_stages,
    greenhouse_jobs_list,
    greenhouse_jobs_update,
)
from tools.users import greenhouse_users_create, greenhouse_users_get, greenhouse_users_list

# =============================================================================
# Action Scopes Decorator
# =============================================================================

F = TypeVar("F", bound=Callable[..., Any])


def action_scopes(action_tool_map: dict[str, Callable]) -> Callable[[F], F]:
    """Decorator that enforces permissions dynamically based on action parameter."""

    def decorator(func: F) -> F:
        # Store action->tool mapping for introspection
        func._action_permissions = action_tool_map  # type: ignore

        # Collect union of all scopes for AuthGuard discovery
        all_scopes: set[str] = set()
        for tool_fn in action_tool_map.values():
            scopes = getattr(tool_fn, "_required_scopes", set())
            all_scopes.update(scopes)
        func._required_scopes = all_scopes  # type: ignore

        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract request from args or kwargs
            request = args[0] if args else kwargs.get("request")
            if request is None:
                raise ToolError("No request provided")

            action = getattr(request, "action", None)
            if action is None:
                raise ToolError("Request has no action field")

            # Help action is always public
            if action == "help":
                return await func(*args, **kwargs)

            # Find the tool function for this action
            tool_fn = action_tool_map.get(action)
            if tool_fn is None:
                # Unknown action - let the function handle it (will return error)
                return await func(*args, **kwargs)

            # Check required scopes for this specific action
            required_scopes = getattr(tool_fn, "_required_scopes", set())
            if required_scopes:
                user = get_current_user()
                if not user:
                    raise AuthorizationError(f"Authentication required for '{action}' action")
                user_scopes = set(user.get("scopes", []))
                if not required_scopes.issubset(user_scopes):
                    missing = required_scopes - user_scopes
                    raise AuthorizationError(
                        f"Missing required scopes for '{action}' action: {missing}"
                    )

            return await func(*args, **kwargs)

        # Preserve attributes on wrapper
        wrapper._action_permissions = func._action_permissions  # type: ignore
        wrapper._required_scopes = func._required_scopes  # type: ignore
        return wrapper  # type: ignore

    return decorator


# =============================================================================
# Help Response Model
# =============================================================================


class HelpResponse(BaseModel):
    """Standard help response for all meta-tools."""

    tool_name: str = Field(..., description="Name of the meta-tool")
    description: str = Field(..., description="Description of what this tool does")
    actions: dict[str, dict[str, Any]] = Field(
        ..., description="Available actions with descriptions and parameters"
    )


# =============================================================================
# Candidates Meta-Tool
# =============================================================================

CANDIDATES_HELP = HelpResponse(
    tool_name="greenhouse_candidates",
    description="Manage candidate profiles in Greenhouse ATS.",
    actions={
        "search": {
            "description": "Search and filter candidates with various criteria",
            "required_params": [],
            "optional_params": [
                "name",
                "email",
                "job_id",
                "tag",
                "created_before",
                "created_after",
                "page",
                "per_page",
            ],
        },
        "get": {
            "description": "Retrieve complete candidate profile by ID",
            "required_params": ["candidate_id"],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new candidate",
            "required_params": ["first_name", "last_name", "email_addresses"],
            "optional_params": [
                "company",
                "title",
                "phone_numbers",
                "addresses",
                "tags",
                "recruiter_id",
            ],
        },
        "update": {
            "description": "Update an existing candidate's fields",
            "required_params": ["candidate_id"],
            "optional_params": ["first_name", "last_name", "company", "title", "is_private"],
        },
        "add_note": {
            "description": "Add a note to a candidate",
            "required_params": ["candidate_id", "user_id", "body"],
            "optional_params": ["visibility"],
        },
        "add_tag": {
            "description": "Add a tag to a candidate's profile",
            "required_params": ["candidate_id", "tag"],
            "optional_params": [],
        },
    },
)


class CandidatesInput(BaseModel):
    """Input for candidates meta-tool."""

    action: Literal["help", "search", "get", "create", "update", "add_note", "add_tag"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    # Common params
    candidate_id: int | None = Field(
        None, description="Candidate ID (required for get/update/add_note/add_tag)"
    )
    # Search params
    name: str | None = Field(None, description="Name filter (partial match)")
    email: str | None = Field(None, description="Email filter (partial match)")
    job_id: int | None = Field(None, description="Filter by job applications")
    tag: str | None = Field(None, description="Filter by tag name")
    created_before: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    created_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    updated_before: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    updated_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    candidate_ids: str | None = Field(None, description="Comma-separated list of candidate IDs")
    page: int = Field(
        1, ge=1, description="Page number (1-indexed). Use with per_page for pagination."
    )
    per_page: int = Field(
        25,
        ge=1,
        le=500,
        description="Results per page (1-500). Default: 25. Use with page for pagination.",
    )
    skip_count: bool = Field(
        False,
        description="Skip total count for faster queries. Set true when you only need results.",
    )
    # Create/Update params
    first_name: str | None = Field(None, description="First name. REQUIRED for create action.")
    last_name: str | None = Field(None, description="Last name. REQUIRED for create action.")
    company: str | None = Field(None, description="Current company")
    title: str | None = Field(None, description="Current job title")
    is_private: bool | None = Field(None, description="Mark candidate as private")
    email_addresses: list[dict[str, str]] | None = Field(
        None, description="Email addresses [{value, type}]"
    )
    phone_numbers: list[dict[str, str]] | None = Field(
        None, description="Phone numbers [{value, type}]"
    )
    addresses: list[dict[str, str]] | None = Field(None, description="Addresses [{value, type}]")
    website_addresses: list[dict[str, str]] | None = Field(None, description="Website addresses")
    social_media_addresses: list[dict[str, str]] | None = Field(
        None, description="Social media addresses"
    )
    tags: list[str] | None = Field(None, description="Tags to add to candidate")
    educations: list[dict[str, Any]] | None = Field(None, description="Education history")
    employments: list[dict[str, Any]] | None = Field(None, description="Employment history")
    recruiter_id: int | None = Field(None, description="Assigned recruiter user ID")
    coordinator_id: int | None = Field(None, description="Assigned coordinator user ID")
    user_id: int | None = Field(None, description="Acting user ID (for notes)")
    # Note params
    body: str | None = Field(None, description="Note body text")
    visibility: str | None = Field(
        None, description="Note visibility (admin_only, public, private)"
    )


class CandidatesOutput(BaseModel):
    """Output for candidates meta-tool."""

    action: str = Field(..., description="Action that was performed")
    help: HelpResponse | None = Field(None, description="Help info (action=help)")
    candidate: CandidateOutput | None = Field(
        None, description="Single candidate (get/create/update)"
    )
    candidates: list[CandidateSearchResultOutput] | None = Field(
        None, description="List of candidates (search)"
    )
    note: CandidateNoteOutput | None = Field(None, description="Created note (add_note)")
    tag_result: AddCandidateTagOutput | None = Field(None, description="Tag result (add_tag)")
    meta: PaginationMeta | None = Field(None, description="Pagination metadata")


@action_scopes(
    {
        "search": greenhouse_candidates_search,
        "get": greenhouse_candidates_get,
        "create": greenhouse_candidates_create,
        "update": greenhouse_candidates_update,
        "add_note": greenhouse_candidates_add_note,
        "add_tag": greenhouse_candidates_add_tag,
    }
)
async def greenhouse_candidates(request: CandidatesInput) -> CandidatesOutput:
    """Manage candidate profiles in Greenhouse ATS."""
    match request.action:
        case "help":
            return CandidatesOutput(action="help", help=CANDIDATES_HELP)

        case "search":
            input_model = SearchCandidatesInput(
                name=request.name,
                email=request.email,
                job_id=request.job_id,
                tag=request.tag,
                created_before=request.created_before,
                created_after=request.created_after,
                updated_before=request.updated_before,
                updated_after=request.updated_after,
                candidate_ids=request.candidate_ids,
                page=request.page,
                per_page=request.per_page,
                skip_count=request.skip_count,
            )
            result = await greenhouse_candidates_search(input_model)
            return CandidatesOutput(action="search", candidates=result.candidates, meta=result.meta)

        case "get":
            if request.candidate_id is None:
                raise ToolError("candidate_id is required for 'get' action")
            input_model = GetCandidateInput(candidate_id=request.candidate_id)
            result = await greenhouse_candidates_get(input_model)
            return CandidatesOutput(action="get", candidate=result)

        case "create":
            if not request.first_name or not request.last_name or not request.email_addresses:
                raise ToolError(
                    "first_name, last_name, and email_addresses are required for 'create'"
                )
            input_model = CreateCandidateInput(
                first_name=request.first_name,
                last_name=request.last_name,
                email_addresses=request.email_addresses,  # type: ignore
                company=request.company,
                title=request.title,
                is_private=request.is_private or False,
                phone_numbers=request.phone_numbers,  # type: ignore
                addresses=request.addresses,  # type: ignore
                website_addresses=request.website_addresses,  # type: ignore
                social_media_addresses=request.social_media_addresses,  # type: ignore
                tags=request.tags,
                educations=request.educations,  # type: ignore
                employments=request.employments,  # type: ignore
                recruiter_id=request.recruiter_id,
                coordinator_id=request.coordinator_id,
                user_id=request.user_id,
            )
            result = await greenhouse_candidates_create(input_model)
            return CandidatesOutput(action="create", candidate=result)

        case "update":
            if request.candidate_id is None:
                raise ToolError("candidate_id is required for 'update' action")
            input_model = UpdateCandidateInput(
                candidate_id=request.candidate_id,
                first_name=request.first_name,
                last_name=request.last_name,
                company=request.company,
                title=request.title,
                is_private=request.is_private,
            )
            result = await greenhouse_candidates_update(input_model)
            return CandidatesOutput(action="update", candidate=result)

        case "add_note":
            if request.candidate_id is None or request.user_id is None or not request.body:
                raise ToolError(
                    "candidate_id, user_id, and body are required for 'add_note' action"
                )
            input_model = AddCandidateNoteInput(
                candidate_id=request.candidate_id,
                user_id=request.user_id,
                body=request.body,
                visibility=request.visibility or "public",  # type: ignore
            )
            result = await greenhouse_candidates_add_note(input_model)
            return CandidatesOutput(action="add_note", note=result)

        case "add_tag":
            if request.candidate_id is None or not request.tag:
                raise ToolError("candidate_id and tag are required for 'add_tag' action")
            input_model = AddCandidateTagInput(
                candidate_id=request.candidate_id,
                tag=request.tag,
            )
            result = await greenhouse_candidates_add_tag(input_model)
            return CandidatesOutput(action="add_tag", tag_result=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Applications Meta-Tool
# =============================================================================

APPLICATIONS_HELP = HelpResponse(
    tool_name="greenhouse_applications",
    description="Manage job applications through the hiring pipeline.",
    actions={
        "list": {
            "description": "List applications with optional filters",
            "required_params": [],
            "optional_params": [
                "job_id",
                "status",
                "candidate_id",
                "current_stage_id",
                "page",
                "per_page",
            ],
        },
        "get": {
            "description": "Retrieve a single application by ID",
            "required_params": ["application_id"],
            "optional_params": [],
        },
        "create": {
            "description": "Create an application for a candidate to a job",
            "required_params": ["candidate_id", "job_id"],
            "optional_params": ["source_id", "initial_stage_id", "recruiter_id", "answers"],
        },
        "advance_stage": {
            "description": "Advance application to next or specified stage",
            "required_params": ["application_id"],
            "optional_params": ["from_stage_id", "to_stage_id"],
        },
        "hire": {
            "description": "Mark an application as hired",
            "required_params": ["application_id"],
            "optional_params": ["opening_id", "start_date", "close_reason_id"],
        },
        "reject": {
            "description": "Reject an application",
            "required_params": ["application_id"],
            "optional_params": ["rejection_reason_id", "notes"],
        },
    },
)


class ApplicationsInput(BaseModel):
    """Input for applications meta-tool."""

    action: Literal["help", "list", "get", "create", "advance_stage", "hire", "reject"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    # Common params
    application_id: int | None = Field(None, description="Application ID")
    # List params
    job_id: int | None = Field(None, description="Filter by job ID")
    status: str | None = Field(None, description="Filter by status (active, hired, rejected)")
    candidate_id: int | None = Field(None, description="Filter by candidate ID")
    current_stage_id: int | None = Field(None, description="Filter by current stage")
    created_before: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    created_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    last_activity_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    page: int = Field(
        1, ge=1, description="Page number (1-indexed). Use with per_page for pagination."
    )
    per_page: int = Field(
        25,
        ge=1,
        le=500,
        description="Results per page (1-500). Default: 25. Use with page for pagination.",
    )
    skip_count: bool = Field(
        False,
        description="Skip total count for faster queries. Set true when you only need results.",
    )
    # Create params
    source_id: int | None = Field(None, description="Source ID for attribution")
    initial_stage_id: int | None = Field(None, description="Starting stage ID")
    recruiter_id: int | None = Field(None, description="Assigned recruiter ID")
    coordinator_id: int | None = Field(None, description="Assigned coordinator ID")
    referrer: ReferrerInput | None = Field(None, description="Referrer information")
    attachments: list[AttachmentInput] | None = Field(None, description="File attachments")
    answers: list[AnswerInput] | None = Field(None, description="Answers to questions")
    # Advance params
    from_stage_id: int | None = Field(None, description="Current stage for validation")
    to_stage_id: int | None = Field(None, description="Target stage ID")
    # Hire params
    opening_id: int | None = Field(None, description="Job opening to close")
    start_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    close_reason_id: int | None = Field(None, description="Close reason ID")
    # Reject params
    rejection_reason_id: int | None = Field(None, description="Rejection reason ID")
    notes: str | None = Field(None, description="Rejection notes")


class ApplicationsOutput(BaseModel):
    """Output for applications meta-tool."""

    action: str
    help: HelpResponse | None = None
    application: ApplicationOutput | None = None
    applications: list[ApplicationOutput] | None = None
    meta: PaginationMeta | None = None


@action_scopes(
    {
        "list": greenhouse_applications_list,
        "get": greenhouse_applications_get,
        "create": greenhouse_applications_create,
        "advance_stage": greenhouse_applications_advance_stage,
        "hire": greenhouse_applications_hire,
        "reject": greenhouse_applications_reject,
    }
)
async def greenhouse_applications(request: ApplicationsInput) -> ApplicationsOutput:
    """Manage job applications through the hiring pipeline."""
    match request.action:
        case "help":
            return ApplicationsOutput(action="help", help=APPLICATIONS_HELP)

        case "list":
            input_model = ListApplicationsInput(
                job_id=request.job_id,
                status=request.status,  # type: ignore
                candidate_id=request.candidate_id,
                current_stage_id=request.current_stage_id,
                created_before=request.created_before,
                created_after=request.created_after,
                last_activity_after=request.last_activity_after,
                page=request.page,
                per_page=request.per_page,
                skip_count=request.skip_count,
            )
            result = await greenhouse_applications_list(input_model)
            return ApplicationsOutput(
                action="list", applications=result.applications, meta=result.meta
            )

        case "get":
            if request.application_id is None:
                raise ToolError("application_id is required for 'get' action")
            input_model = GetApplicationInput(application_id=request.application_id)
            result = await greenhouse_applications_get(input_model)
            return ApplicationsOutput(action="get", application=result)

        case "create":
            if request.candidate_id is None or request.job_id is None:
                raise ToolError("candidate_id and job_id are required for 'create' action")
            input_model = CreateApplicationInput(
                candidate_id=request.candidate_id,
                job_id=request.job_id,
                source_id=request.source_id,
                initial_stage_id=request.initial_stage_id,
                recruiter_id=request.recruiter_id,
                coordinator_id=request.coordinator_id,
                referrer=request.referrer,
                attachments=request.attachments,
                answers=request.answers,  # type: ignore
            )
            result = await greenhouse_applications_create(input_model)
            return ApplicationsOutput(action="create", application=result)

        case "advance_stage":
            if request.application_id is None:
                raise ToolError("application_id is required for 'advance_stage' action")
            input_model = AdvanceApplicationInput(
                application_id=request.application_id,
                from_stage_id=request.from_stage_id,
                to_stage_id=request.to_stage_id,
            )
            result = await greenhouse_applications_advance_stage(input_model)
            return ApplicationsOutput(action="advance_stage", application=result)

        case "hire":
            if request.application_id is None:
                raise ToolError("application_id is required for 'hire' action")
            input_model = HireApplicationInput(
                application_id=request.application_id,
                opening_id=request.opening_id,
                start_date=request.start_date,
                close_reason_id=request.close_reason_id,
            )
            result = await greenhouse_applications_hire(input_model)
            return ApplicationsOutput(action="hire", application=result)

        case "reject":
            if request.application_id is None:
                raise ToolError("application_id is required for 'reject' action")
            input_model = RejectApplicationInput(
                application_id=request.application_id,
                rejection_reason_id=request.rejection_reason_id,
                notes=request.notes,
            )
            result = await greenhouse_applications_reject(input_model)
            return ApplicationsOutput(action="reject", application=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Jobs Meta-Tool
# =============================================================================

JOBS_HELP = HelpResponse(
    tool_name="greenhouse_jobs",
    description="Manage job requisitions and pipeline stages.",
    actions={
        "list": {
            "description": "List jobs with optional filters",
            "required_params": [],
            "optional_params": ["status", "department_id", "office_id", "page", "per_page"],
        },
        "get": {
            "description": "Retrieve a single job by ID",
            "required_params": ["job_id"],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new job with default pipeline stages",
            "required_params": ["name"],
            "optional_params": ["department_id", "office_ids", "status", "notes"],
        },
        "update": {
            "description": "Update an existing job with PATCH semantics",
            "required_params": ["job_id"],
            "optional_params": [
                "name",
                "requisition_id",
                "notes",
                "status",
                "department_id",
                "office_ids",
            ],
        },
        "get_stages": {
            "description": "Get pipeline stages for a job",
            "required_params": ["job_id"],
            "optional_params": [],
        },
    },
)


class JobsInput(BaseModel):
    """Input for jobs meta-tool."""

    action: Literal["help", "list", "get", "create", "update", "get_stages"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    # Common params
    job_id: int | None = Field(None, description="Job ID")
    # List params
    status: str | None = Field(None, description="Filter by status (open, closed, draft)")
    department_id: int | None = Field(None, description="Filter by department")
    office_id: int | None = Field(None, description="Filter by office")
    requisition_id: str | None = Field(None, description="Filter by requisition ID")
    created_before: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    created_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    updated_before: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    updated_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    page: int = Field(
        1, ge=1, description="Page number (1-indexed). Use with per_page for pagination."
    )
    per_page: int = Field(
        25,
        ge=1,
        le=500,
        description="Results per page (1-500). Default: 25. Use with page for pagination.",
    )
    skip_count: bool = Field(
        False,
        description="Skip total count for faster queries. Set true when you only need results.",
    )
    # Create params
    name: str | None = Field(None, description="Job name/title")
    template_job_id: int | None = Field(None, description="Template job ID to copy from")
    notes: str | None = Field(None, description="Internal notes")
    anywhere: bool | None = Field(None, description="Remote/anywhere position")
    office_ids: list[int] | None = Field(None, description="Office IDs")
    opening_ids: list[str] | None = Field(None, description="External opening IDs for tracking")
    number_of_openings: int | None = Field(None, description="Number of openings")


class JobsOutput(BaseModel):
    """Output for jobs meta-tool."""

    action: str
    help: HelpResponse | None = None
    job: JobOutput | None = None
    jobs: list[JobOutput] | None = None
    stages: list[JobStageOutput] | None = None
    meta: PaginationMeta | None = None


@action_scopes(
    {
        "list": greenhouse_jobs_list,
        "get": greenhouse_jobs_get,
        "create": greenhouse_jobs_create,
        "update": greenhouse_jobs_update,
        "get_stages": greenhouse_jobs_get_stages,
    }
)
async def greenhouse_jobs(request: JobsInput) -> JobsOutput:
    """Manage job requisitions and pipeline stages."""
    match request.action:
        case "help":
            return JobsOutput(action="help", help=JOBS_HELP)

        case "list":
            input_model = ListJobsInput(
                status=request.status,  # type: ignore
                department_id=request.department_id,
                office_id=request.office_id,
                requisition_id=request.requisition_id,
                created_before=request.created_before,
                created_after=request.created_after,
                updated_before=request.updated_before,
                updated_after=request.updated_after,
                page=request.page,
                per_page=request.per_page,
                skip_count=request.skip_count,
            )
            result = await greenhouse_jobs_list(input_model)
            # Convert dict jobs to JobOutput models
            jobs = [JobOutput.model_validate(j) for j in result.jobs]
            return JobsOutput(action="list", jobs=jobs, meta=result.meta)

        case "get":
            if request.job_id is None:
                raise ToolError("job_id is required for 'get' action")
            input_model = GetJobInput(job_id=request.job_id)
            result = await greenhouse_jobs_get(input_model)
            return JobsOutput(action="get", job=result)

        case "create":
            if not request.name:
                raise ToolError("name is required for 'create' action")
            input_model = CreateJobInput(
                name=request.name,
                template_job_id=request.template_job_id,
                requisition_id=request.requisition_id,
                notes=request.notes,
                anywhere=request.anywhere or False,
                department_id=request.department_id,
                office_ids=request.office_ids,
                opening_ids=request.opening_ids,
                number_of_openings=request.number_of_openings or 1,
                status=request.status or "draft",  # type: ignore
            )
            result = await greenhouse_jobs_create(input_model)
            return JobsOutput(action="create", job=result)

        case "update":
            if request.job_id is None:
                raise ToolError("job_id is required for 'update' action")
            input_model = UpdateJobInput(
                job_id=request.job_id,
                name=request.name,
                requisition_id=request.requisition_id,
                notes=request.notes,
                status=request.status,  # type: ignore
                department_id=request.department_id,
                office_ids=request.office_ids,
            )
            result = await greenhouse_jobs_update(input_model)
            return JobsOutput(action="update", job=result)

        case "get_stages":
            if request.job_id is None:
                raise ToolError("job_id is required for 'get_stages' action")
            input_model = GetJobStagesInput(
                job_id=request.job_id,
                created_before=request.created_before,
                created_after=request.created_after,
                updated_before=request.updated_before,
                updated_after=request.updated_after,
            )
            result = await greenhouse_jobs_get_stages(input_model)
            return JobsOutput(action="get_stages", stages=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Feedback Meta-Tool
# =============================================================================

FEEDBACK_HELP = HelpResponse(
    tool_name="greenhouse_feedback",
    description="Submit and retrieve interview feedback (scorecards).",
    actions={
        "list": {
            "description": "List scorecards for an application",
            "required_params": ["application_id"],
            "optional_params": ["page", "per_page"],
        },
        "submit": {
            "description": "Submit interview feedback/scorecard",
            "required_params": ["application_id", "interviewer_id", "overall_recommendation"],
            "optional_params": ["interview_step_id", "interviewed_at", "attributes", "questions"],
        },
    },
)


class FeedbackInput(BaseModel):
    """Input for feedback meta-tool."""

    action: Literal["help", "list", "submit"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    # Common params
    application_id: int | None = Field(None, description="Application ID")
    # List params
    page: int = Field(
        1, ge=1, description="Page number (1-indexed). Use with per_page for pagination."
    )
    per_page: int = Field(
        100,
        ge=1,
        le=500,
        description="Results per page (1-500). Default: 25. Use with page for pagination.",
    )
    # Submit params
    interviewer_id: int | None = Field(None, description="Interviewer user ID")
    interview_step_id: int | None = Field(None, description="Interview step ID")
    overall_recommendation: str | None = Field(
        None, description="Rating: definitely_not, no, mixed, yes, strong_yes, no_decision"
    )
    interviewed_at: str | None = Field(None, description="Interview timestamp (ISO 8601)")
    attributes: list[dict[str, Any]] | None = Field(None, description="Attribute ratings")
    questions: list[dict[str, Any]] | None = Field(None, description="Interview questions/answers")


class FeedbackOutput(BaseModel):
    """Output for feedback meta-tool."""

    action: str
    help: HelpResponse | None = None
    scorecard: ScorecardOutput | None = None
    scorecards: list[ScorecardOutput] | None = None


@action_scopes(
    {
        "list": greenhouse_feedback_list,
        "submit": greenhouse_feedback_submit,
    }
)
async def greenhouse_feedback(request: FeedbackInput) -> FeedbackOutput:
    """Submit and retrieve interview feedback (scorecards)."""
    match request.action:
        case "help":
            return FeedbackOutput(action="help", help=FEEDBACK_HELP)

        case "list":
            if request.application_id is None:
                raise ToolError("application_id is required for 'list' action")
            input_model = ListFeedbackInput(
                application_id=request.application_id,
                page=request.page,
                per_page=request.per_page,
            )
            result = await greenhouse_feedback_list(input_model)
            return FeedbackOutput(action="list", scorecards=result.scorecards)

        case "submit":
            if (
                request.application_id is None
                or request.interviewer_id is None
                or not request.overall_recommendation
            ):
                raise ToolError(
                    "application_id, interviewer_id, and overall_recommendation "
                    "are required for 'submit' action"
                )
            input_model = SubmitFeedbackInput(
                application_id=request.application_id,
                interviewer_id=request.interviewer_id,
                overall_recommendation=request.overall_recommendation,  # type: ignore
                interview_step_id=request.interview_step_id,
                interviewed_at=request.interviewed_at,
                attributes=request.attributes,  # type: ignore
                questions=request.questions,  # type: ignore
            )
            result = await greenhouse_feedback_submit(input_model)
            return FeedbackOutput(action="submit", scorecard=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Users Meta-Tool
# =============================================================================

USERS_HELP = HelpResponse(
    tool_name="greenhouse_users",
    description="Manage system users (recruiters, hiring managers, etc.).",
    actions={
        "list": {
            "description": "List users with optional filters",
            "required_params": [],
            "optional_params": ["email", "employee_id", "page", "per_page"],
        },
        "get": {
            "description": "Retrieve a single user by ID",
            "required_params": ["user_id"],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new user with Basic permissions",
            "required_params": ["first_name", "last_name", "email"],
            "optional_params": ["employee_id", "office_ids", "department_ids"],
        },
    },
)


class UsersInput(BaseModel):
    """Input for users meta-tool."""

    action: Literal["help", "list", "get", "create"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    # Common params
    user_id: int | None = Field(None, description="User ID")
    # List params (also used by create)
    email: str | None = Field(None, description="Filter by email (list) or user email (create)")
    employee_id: str | None = Field(
        None, description="Filter by employee ID (list) or set employee ID (create)"
    )
    created_before: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    created_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    updated_before: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    updated_after: str | None = Field(
        None, description="ISO 8601 date filter (e.g., 2024-01-15T00:00:00Z)."
    )
    page: int = Field(
        1, ge=1, description="Page number (1-indexed). Use with per_page for pagination."
    )
    per_page: int = Field(
        25,
        ge=1,
        le=500,
        description="Results per page (1-500). Default: 25. Use with page for pagination.",
    )
    skip_count: bool = Field(
        False,
        description="Skip total count for faster queries. Set true when you only need results.",
    )
    # Create params
    first_name: str | None = Field(None, description="User's first name. REQUIRED for create.")
    last_name: str | None = Field(None, description="User's last name. REQUIRED for create.")
    office_ids: list[int] | None = Field(None, description="Office ID(s) to associate with user")
    department_ids: list[int] | None = Field(
        None, description="Department ID(s) to associate with user"
    )


class UsersOutput(BaseModel):
    """Output for users meta-tool."""

    action: str
    help: HelpResponse | None = None
    user: UserOutput | None = None
    users: list[UserOutput] | None = None
    meta: PaginationMeta | None = None


@action_scopes(
    {
        "list": greenhouse_users_list,
        "get": greenhouse_users_get,
        "create": greenhouse_users_create,
    }
)
async def greenhouse_users(request: UsersInput) -> UsersOutput:
    """Query system users (recruiters, hiring managers, etc.)."""
    match request.action:
        case "help":
            return UsersOutput(action="help", help=USERS_HELP)

        case "list":
            input_model = ListUsersInput(
                email=request.email,
                employee_id=request.employee_id,
                created_before=request.created_before,
                created_after=request.created_after,
                updated_before=request.updated_before,
                updated_after=request.updated_after,
                page=request.page,
                per_page=request.per_page,
                skip_count=request.skip_count,
            )
            result = await greenhouse_users_list(input_model)
            return UsersOutput(action="list", users=result.users, meta=result.meta)

        case "get":
            if request.user_id is None:
                raise ToolError("user_id is required for 'get' action")
            input_model = GetUserInput(user_id=request.user_id)
            result = await greenhouse_users_get(input_model)
            return UsersOutput(action="get", user=result)

        case "create":
            if not request.first_name or not request.last_name or not request.email:
                raise ToolError("first_name, last_name, and email are required for 'create' action")
            input_model = CreateUserInput(
                first_name=request.first_name,
                last_name=request.last_name,
                email=request.email,
                employee_id=request.employee_id,
                office_ids=request.office_ids,
                department_ids=request.department_ids,
            )
            result = await greenhouse_users_create(input_model)
            return UsersOutput(action="create", user=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Activity Meta-Tool
# =============================================================================

ACTIVITY_HELP = HelpResponse(
    tool_name="greenhouse_activity",
    description="Retrieve candidate activity feeds (notes, emails, events).",
    actions={
        "get": {
            "description": "Get activity feed for a candidate",
            "required_params": ["candidate_id"],
            "optional_params": [],
        },
    },
)


class ActivityInput(BaseModel):
    """Input for activity meta-tool."""

    action: Literal["help", "get"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    candidate_id: int | None = Field(None, description="Candidate ID")


class ActivityOutput(BaseModel):
    """Output for activity meta-tool."""

    action: str
    help: HelpResponse | None = None
    activity: ActivityFeedOutput | None = None


@action_scopes(
    {
        "get": greenhouse_activity_get,
    }
)
async def greenhouse_activity(request: ActivityInput) -> ActivityOutput:
    """Retrieve candidate activity feeds (notes, emails, events)."""
    match request.action:
        case "help":
            return ActivityOutput(action="help", help=ACTIVITY_HELP)

        case "get":
            if request.candidate_id is None:
                raise ToolError("candidate_id is required for 'get' action")
            input_model = GetActivityFeedInput(candidate_id=request.candidate_id)
            result = await greenhouse_activity_get(input_model)
            return ActivityOutput(action="get", activity=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Jobboard Meta-Tool (Public)
# =============================================================================

JOBBOARD_HELP = HelpResponse(
    tool_name="greenhouse_jobboard",
    description="Public job board for browsing and applying to jobs.",
    actions={
        "list_jobs": {
            "description": "List all jobs on the public job board",
            "required_params": [],
            "optional_params": ["content"],
        },
        "apply": {
            "description": "Submit a job application (candidate self-service)",
            "required_params": ["job_post_id", "first_name", "last_name", "email"],
            "optional_params": [
                "phone",
                "resume_text",
                "cover_letter_text",
                "educations",
                "employments",
            ],
        },
    },
)


class JobboardInput(BaseModel):
    """Input for jobboard meta-tool."""

    action: Literal["help", "list_jobs", "apply"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    # List params
    content: bool = Field(False, description="Include job content in response")
    # Apply params - from JobBoardApplyInput
    job_post_id: int | None = Field(None, description="Job post ID")
    first_name: str | None = Field(None, description="Applicant first name")
    last_name: str | None = Field(None, description="Applicant last name")
    email: str | None = Field(None, description="Applicant email")
    phone: str | None = Field(None, description="Phone number")
    location: str | None = Field(None, description="Location")
    latitude: str | None = Field(None, description="Location latitude")
    longitude: str | None = Field(None, description="Location longitude")
    resume_text: str | None = Field(None, description="Resume as plain text")
    resume_url: str | None = Field(None, description="Resume URL")
    cover_letter_text: str | None = Field(None, description="Cover letter text")
    educations: list[dict[str, Any]] | None = Field(None, description="Education history")
    employments: list[dict[str, Any]] | None = Field(None, description="Employment history")
    answers: list[dict[str, Any]] | None = Field(None, description="Answers to job questions")
    mapped_url_token: str | None = Field(None, description="URL token for tracking")


class JobboardOutput(BaseModel):
    """Output for jobboard meta-tool."""

    action: str
    help: HelpResponse | None = None
    jobs: list[dict[str, Any]] | None = None
    apply_result: JobBoardApplyOutput | None = None
    meta: dict[str, Any] | None = None


@public_tool
async def greenhouse_jobboard(request: JobboardInput) -> JobboardOutput:
    """Public job board for browsing and applying to jobs."""
    match request.action:
        case "help":
            return JobboardOutput(action="help", help=JOBBOARD_HELP)

        case "list_jobs":
            input_model = ListJobBoardJobsInput(content=request.content)
            result = await greenhouse_jobboard_list_jobs(input_model)
            return JobboardOutput(action="list_jobs", jobs=result.jobs, meta=result.meta)

        case "apply":
            if (
                not request.job_post_id
                or not request.first_name
                or not request.last_name
                or not request.email
            ):
                raise ToolError(
                    "job_post_id, first_name, last_name, and email are required for 'apply' action"
                )
            input_model = JobBoardApplyInput(
                job_post_id=request.job_post_id,
                first_name=request.first_name,
                last_name=request.last_name,
                email=request.email,
                phone=request.phone,
                location=request.location,
                latitude=request.latitude,
                longitude=request.longitude,
                resume_text=request.resume_text,
                resume_url=request.resume_url,
                cover_letter_text=request.cover_letter_text,
                educations=request.educations,  # type: ignore
                employments=request.employments,  # type: ignore
                answers=request.answers,
                mapped_url_token=request.mapped_url_token,
            )
            result = await greenhouse_jobboard_apply(input_model)
            return JobboardOutput(action="apply", apply_result=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Admin Meta-Tool
# =============================================================================

ADMIN_HELP = HelpResponse(
    tool_name="greenhouse_admin",
    description="Administrative tools for database management.",
    actions={
        "reset_state": {
            "description": "Clear all data and reset database to empty state",
            "required_params": ["confirm"],
            "optional_params": ["clear_users"],
        },
        "export_snapshot": {
            "description": "Export database snapshot as ZIP file",
            "required_params": [],
            "optional_params": ["include_schema"],
        },
    },
)


class AdminInput(BaseModel):
    """Input for admin meta-tool."""

    action: Literal["help", "reset_state", "export_snapshot"] = Field(
        ..., description="Action to perform. REQUIRED. Use help to see available actions."
    )
    # Reset params
    confirm: bool = Field(False, description="Confirm reset (required)")
    clear_users: bool = Field(True, description="Clear user accounts (default True)")
    # Export params
    include_schema: bool = Field(True, description="Include schema DDL in export")


class AdminOutput(BaseModel):
    """Output for admin meta-tool."""

    action: str
    help: HelpResponse | None = None
    reset_result: GreenhouseResetStateResponse | None = None
    export_result: ExportSnapshotOutput | None = None


@action_scopes(
    {
        "reset_state": greenhouse_reset_state,
        "export_snapshot": greenhouse_export_snapshot,
    }
)
async def greenhouse_admin(request: AdminInput) -> AdminOutput:
    """Administrative tools for database management."""
    match request.action:
        case "help":
            return AdminOutput(action="help", help=ADMIN_HELP)

        case "reset_state":
            input_model = GreenhouseResetStateInput(
                confirm=request.confirm, clear_users=request.clear_users
            )
            result = await greenhouse_reset_state(input_model)
            return AdminOutput(action="reset_state", reset_result=result)

        case "export_snapshot":
            input_model = ExportSnapshotInput(include_schema=request.include_schema)
            result = await greenhouse_export_snapshot(input_model)
            return AdminOutput(action="export_snapshot", export_result=result)

    raise ToolError(f"Unknown action: {request.action}")


# =============================================================================
# Schema Introspection Tool
# =============================================================================

TOOL_SCHEMAS: dict[str, dict[str, type[BaseModel]]] = {
    "greenhouse_candidates": {"input": CandidatesInput, "output": CandidatesOutput},
    "greenhouse_applications": {"input": ApplicationsInput, "output": ApplicationsOutput},
    "greenhouse_jobs": {"input": JobsInput, "output": JobsOutput},
    "greenhouse_feedback": {"input": FeedbackInput, "output": FeedbackOutput},
    "greenhouse_users": {"input": UsersInput, "output": UsersOutput},
    "greenhouse_activity": {"input": ActivityInput, "output": ActivityOutput},
    "greenhouse_jobboard": {"input": JobboardInput, "output": JobboardOutput},
    "greenhouse_admin": {"input": AdminInput, "output": AdminOutput},
}


class SchemaInput(BaseModel):
    """Input for schema introspection tool."""

    tool: str | None = Field(None, description="Tool name to get schema for (omit for list)")


class SchemaOutput(BaseModel):
    """Output for schema introspection tool."""

    tools: list[str] | None = Field(
        None, description="List of available tools (when tool not specified)"
    )
    tool: str | None = Field(None, description="Tool name")
    input_schema: dict[str, Any] | None = Field(None, description="JSON Schema for tool input")
    output_schema: dict[str, Any] | None = Field(None, description="JSON Schema for tool output")


@public_tool
async def greenhouse_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for any meta-tool's input/output."""
    if request.tool is None:
        return SchemaOutput(tools=list(TOOL_SCHEMAS.keys()))

    if request.tool not in TOOL_SCHEMAS:
        raise ToolError(f"Unknown tool: {request.tool}. Available: {list(TOOL_SCHEMAS.keys())}")

    schemas = TOOL_SCHEMAS[request.tool]
    return SchemaOutput(
        tool=request.tool,
        input_schema=schemas["input"].model_json_schema(),
        output_schema=schemas["output"].model_json_schema(),
    )


# =============================================================================
# Registration
# =============================================================================


def register_meta_tools(mcp: FastMCP) -> None:
    """Register all meta-tools for LLM interface."""
    mcp.tool()(greenhouse_candidates)
    mcp.tool()(greenhouse_applications)
    mcp.tool()(greenhouse_jobs)
    mcp.tool()(greenhouse_feedback)
    mcp.tool()(greenhouse_users)
    mcp.tool()(greenhouse_activity)
    mcp.tool()(greenhouse_jobboard)
    mcp.tool()(greenhouse_admin)
    mcp.tool()(greenhouse_schema)
