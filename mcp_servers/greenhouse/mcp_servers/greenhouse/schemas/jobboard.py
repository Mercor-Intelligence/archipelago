"""Job Board API Pydantic models for Greenhouse MCP Server.

Input and output schemas for job board tools:
- greenhouse_jobboard_list_jobs
- greenhouse_jobboard_apply
- greenhouse_jobboard_create_post
"""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field
from schemas.common import EducationInput, Email, EmploymentInput

# =============================================================================
# Output Models
# =============================================================================


class JobBoardApplyOutput(BaseModel):
    """Output for job board application submission."""

    success: bool = Field(..., description="Whether application was submitted successfully")
    status: str = Field(..., description="Application status (e.g., 'created', 'duplicate')")
    application_id: int = Field(..., description="ID of the created application")
    candidate_id: int = Field(..., description="ID of the candidate (created or existing)")


class ListJobBoardJobsOutput(BaseModel):
    """Output for listing job board jobs."""

    jobs: list[dict[str, Any]] = Field(..., description="List of public job postings")
    meta: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata about the response"
    )


# =============================================================================
# Input Models
# =============================================================================


class ListJobBoardJobsInput(BaseModel):
    """Input for listing publicly available jobs from the job board.

    Tool: greenhouse_jobboard_list_jobs
    API: GET /boards/{board_token}/jobs

    Note: This is a public endpoint that doesn't require authentication.
    """

    content: bool = Field(default=False, description="Include full job description in response")


class JobBoardApplyInput(BaseModel):
    """Input for submitting a job board application.

    Tool: greenhouse_jobboard_apply
    API: POST /boards/{board_token}/jobs/{id}

    Note: This simulates the candidate self-apply flow from a public job board.
    Required fields are first_name, last_name, and email.
    """

    job_post_id: int = Field(..., description="Job post ID from job board (not internal_job_id)")
    first_name: str = Field(..., description="Applicant's first name")
    last_name: str = Field(..., description="Applicant's last name")
    email: Email = Field(..., description="Applicant's email address")
    phone: str | None = Field(default=None, description="Applicant's phone number")
    location: str | None = Field(default=None, description="Applicant's location/address")
    latitude: str | None = Field(default=None, description="Hidden field for location latitude")
    longitude: str | None = Field(default=None, description="Hidden field for location longitude")
    resume_text: str | None = Field(default=None, description="Resume content as plain text")
    resume_url: str | None = Field(default=None, description="URL to hosted resume file")
    cover_letter_text: str | None = Field(default=None, description="Cover letter as plain text")
    educations: list[EducationInput] | None = Field(
        default=None, description="Education history entries"
    )
    employments: list[EmploymentInput] | None = Field(
        default=None, description="Employment history entries"
    )
    mapped_url_token: str | None = Field(
        default=None, description="gh_src tracking parameter for attribution"
    )
    answers: list[dict] | None = Field(
        default=None,
        description="Answers to job post questions. Format: [{'question': '...', 'answer': '...'}]",
    )
    # Note: Dynamic question fields (question_{id}) would be handled at runtime


class CreateJobPostInput(BaseModel):
    """Input for creating a job post on the public job board.

    Tool: greenhouse_jobboard_create_post
    API: POST /job_posts

    Creates a public-facing job posting linked to an internal job requisition.
    The post must be marked as live to appear on the public job board.
    """

    job_id: int = Field(
        ...,
        description="Internal job ID to create a posting for",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_list",
            "x-populate-field": "jobs",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    title: str = Field(..., description="Job post title (displayed on job board)")
    location_name: str | None = Field(
        default=None, description="Location string (e.g., 'San Francisco, CA')"
    )
    content: str | None = Field(default=None, description="Job description HTML content")
    live: bool = Field(default=True, description="Whether the post is live on the public board")
    internal: bool = Field(default=False, description="Whether this is an internal-only posting")


class CreateJobPostOutput(BaseModel):
    """Output for job post creation."""

    id: int = Field(..., description="Created job post ID")
    job_id: int = Field(..., description="Internal job ID")
    title: str = Field(..., description="Job post title")
    location_name: str | None = Field(default=None, description="Location")
    content: str | None = Field(default=None, description="Job description")
    live: bool = Field(..., description="Whether the post is live")
    internal: bool = Field(..., description="Whether this is internal-only")
