"""Job-related Pydantic models for Greenhouse MCP Server.

Input and output schemas for job tools:
- greenhouse_jobs_list
- greenhouse_jobs_get
- greenhouse_jobs_get_stages
- greenhouse_jobs_create
- greenhouse_jobs_update
"""

from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field

from .common import PaginationMeta

# =============================================================================
# Output Models
# =============================================================================


class HiringTeamMemberOutput(BaseModel):
    """Member of a job's hiring team."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    employee_id: str | None = None
    responsible: bool = False


class HiringTeamOutput(BaseModel):
    """Hiring team for a job."""

    hiring_managers: list[HiringTeamMemberOutput] = Field(default_factory=list)
    recruiters: list[HiringTeamMemberOutput] = Field(default_factory=list)
    coordinators: list[HiringTeamMemberOutput] = Field(default_factory=list)


class JobOpeningOutput(BaseModel):
    """Opening (position slot) for a job."""

    id: int
    opening_id: str | None = None
    status: str
    opened_at: str | None = None
    closed_at: str | None = None
    application_id: int | None = None
    close_reason: dict[str, Any] | None = None


class JobDepartmentOutput(BaseModel):
    """Department associated with a job."""

    id: int
    name: str
    parent_id: int | None = None
    child_ids: list[int] = Field(default_factory=list)
    external_id: str | None = None


class JobOfficeLocationOutput(BaseModel):
    """Location info for an office."""

    name: str | None = None


class JobOfficeOutput(BaseModel):
    """Office associated with a job."""

    id: int
    name: str
    location: JobOfficeLocationOutput | None = None
    primary_contact_user_id: int | None = None
    parent_id: int | None = None
    child_ids: list[int] = Field(default_factory=list)
    external_id: str | None = None


class JobOutput(BaseModel):
    """Full job object returned by get/create operations."""

    id: int
    name: str
    requisition_id: str | None = None
    notes: str | None = None
    confidential: bool = False
    status: str
    opened_at: str | None = None
    closed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    is_template: bool = False
    copied_from_id: int | None = None
    departments: list[JobDepartmentOutput] = Field(default_factory=list)
    offices: list[JobOfficeOutput] = Field(default_factory=list)
    hiring_team: HiringTeamOutput = Field(default_factory=HiringTeamOutput)
    openings: list[JobOpeningOutput] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    keyed_custom_fields: dict[str, Any] = Field(default_factory=dict)


class ListJobsOutput(BaseModel):
    """Output for list jobs endpoint with pagination."""

    jobs: list[dict[str, Any]] = Field(..., description="List of job objects")
    meta: PaginationMeta = Field(..., description="Pagination metadata")


class InterviewKitQuestionOutput(BaseModel):
    """Question in an interview kit."""

    id: int
    question: str


class InterviewKitOutput(BaseModel):
    """Interview kit with content and questions."""

    id: int | None
    content: str | None
    questions: list[InterviewKitQuestionOutput]


class DefaultInterviewerUserOutput(BaseModel):
    """Default interviewer user info."""

    id: int
    first_name: str | None
    last_name: str | None
    name: str | None
    employee_id: str | None


class InterviewOutput(BaseModel):
    """Interview step within a job stage."""

    id: int
    name: str
    schedulable: bool
    estimated_minutes: int
    default_interviewer_users: list[DefaultInterviewerUserOutput]
    interview_kit: InterviewKitOutput


class JobStageOutput(BaseModel):
    """Pipeline stage for a job."""

    id: int
    name: str
    created_at: str | None
    updated_at: str | None
    active: bool
    job_id: int
    priority: int
    interviews: list[InterviewOutput]


# =============================================================================
# Input Models
# =============================================================================


class ListJobsInput(BaseModel):
    """Input for listing jobs with filters.

    Tool: greenhouse_jobs_list
    API: GET /jobs
    """

    per_page: int = Field(
        default=100, ge=1, le=500, description="Number of results per page (max 500)"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    skip_count: bool = Field(
        default=False, description="Skip total count calculation for performance"
    )
    status: Literal["open", "closed", "draft"] | None = Field(
        default=None, description="Filter by job status"
    )
    department_id: int | None = Field(
        default=None,
        description="Filter by department ID",
        json_schema_extra={
            "x-populate-from": "greenhouse_departments_list",
            "x-populate-field": "departments",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    office_id: int | None = Field(
        default=None,
        description="Filter by office ID",
        json_schema_extra={
            "x-populate-from": "greenhouse_offices_list",
            "x-populate-field": "offices",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    requisition_id: str | None = Field(
        default=None, description="Filter by requisition ID (external reference)"
    )
    created_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    created_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")


class GetJobInput(BaseModel):
    """Input for retrieving a single job.

    Tool: greenhouse_jobs_get
    API: GET /jobs/{id}
    """

    job_id: int = Field(
        ...,
        description="Job ID to retrieve",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_list",
            "x-populate-field": "jobs",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )


class GetJobStagesInput(BaseModel):
    """Input for retrieving job pipeline stages.

    Tool: greenhouse_jobs_get_stages
    API: GET /jobs/{id}/stages
    """

    job_id: int = Field(
        ...,
        description="Job ID to get stages for",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_list",
            "x-populate-field": "jobs",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    created_before: str | None = Field(
        default=None, description="Filter stages created before this ISO 8601 timestamp"
    )
    created_after: str | None = Field(
        default=None, description="Filter stages created after this ISO 8601 timestamp"
    )
    updated_before: str | None = Field(
        default=None, description="Filter stages updated before this ISO 8601 timestamp"
    )
    updated_after: str | None = Field(
        default=None, description="Filter stages updated after this ISO 8601 timestamp"
    )


class CreateJobInput(BaseModel):
    """Input for creating a new job.

    Tool: greenhouse_jobs_create
    API: POST /jobs
    """

    template_job_id: int | None = Field(
        default=None,
        description="Job ID to copy from",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_list",
            "x-populate-field": "jobs",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    name: str = Field(..., description="Job title")
    requisition_id: str | None = Field(default=None, description="External requisition ID")
    notes: str | None = Field(default=None, description="Internal notes about the job")
    anywhere: bool = Field(default=False, description="Whether job can be performed anywhere")
    department_id: int | None = Field(
        default=None,
        description="Department ID for the job",
        json_schema_extra={
            "x-populate-from": "greenhouse_departments_list",
            "x-populate-field": "departments",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    office_ids: list[int] | None = Field(
        default=None,
        description="List of office IDs where the job is located",
        json_schema_extra={
            "x-populate-from": "greenhouse_offices_list",
            "x-populate-field": "offices",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    opening_ids: list[str] | None = Field(
        default=None, description="External opening IDs for tracking"
    )
    number_of_openings: int = Field(default=1, ge=1, description="Number of openings for this job")
    status: Literal["open", "closed", "draft"] = Field(
        default="draft", description="Job status (open, closed, or draft)"
    )


class UpdateJobInput(BaseModel):
    """Input for updating an existing job.

    Tool: greenhouse_jobs_update
    API: PATCH /jobs/{id}

    All fields except job_id are optional.
    """

    job_id: int = Field(
        ...,
        description="Job ID to update",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_list",
            "x-populate-field": "jobs",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    name: str | None = Field(default=None, description="Updated job title")
    requisition_id: str | None = Field(default=None, description="Updated requisition ID")
    notes: str | None = Field(default=None, description="Updated internal notes")
    status: Literal["open", "closed", "draft"] | None = Field(
        default=None, description="Updated job status"
    )
    department_id: int | None = Field(
        default=None,
        description="Updated department ID (replaces existing)",
        json_schema_extra={
            "x-populate-from": "greenhouse_departments_list",
            "x-populate-field": "departments",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    office_ids: list[int] | None = Field(
        default=None,
        description="Updated office IDs (replaces existing)",
        json_schema_extra={
            "x-populate-from": "greenhouse_offices_list",
            "x-populate-field": "offices",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
