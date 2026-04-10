"""Application-related Pydantic models for Greenhouse MCP Server.

Defines input and output schemas for application tools:
- greenhouse_applications_get
- greenhouse_applications_list
- greenhouse_applications_create
- greenhouse_applications_advance_stage
- greenhouse_applications_reject
- greenhouse_applications_hire
"""

from typing import Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field
from schemas.common import PaginationMeta


class ApplicationStageOutput(BaseModel):
    """Current stage info in application responses."""

    id: int
    name: str


class ApplicationJobOutput(BaseModel):
    """Job info included in application responses."""

    id: int
    name: str


class ApplicationSourceOutput(BaseModel):
    """Source attribution for the application."""

    id: int
    public_name: str | None


class ApplicationCreditedToOutput(BaseModel):
    """User credited for the application."""

    id: int
    name: str | None


class RejectionReasonTypeOutput(BaseModel):
    """Rejection reason type info."""

    id: int | None
    name: str | None


class ApplicationRejectionReasonOutput(BaseModel):
    """Rejection reason info for rejected applications."""

    id: int
    name: str
    type: RejectionReasonTypeOutput


class ApplicationOutput(BaseModel):
    """Simplified application representation returned by greenhouse_applications_list."""

    id: int
    candidate_id: int
    prospect: bool
    applied_at: str | None
    rejected_at: str | None
    hired_at: str | None = None
    last_activity_at: str | None
    status: str
    current_stage: ApplicationStageOutput | None
    jobs: list[ApplicationJobOutput]
    source: ApplicationSourceOutput | None
    credited_to: ApplicationCreditedToOutput | None
    rejection_reason: ApplicationRejectionReasonOutput | None = None


class ListApplicationsOutput(BaseModel):
    """Response payload for greenhouse_applications_list."""

    applications: list[ApplicationOutput]
    meta: PaginationMeta


# =============================================================================
# Input Models
# =============================================================================


class GetApplicationInput(BaseModel):
    """Input for retrieving a single application.

    Tool: greenhouse_applications_get
    API: GET /applications/{id}
    """

    application_id: int = Field(..., description="Application ID to retrieve")


class ListApplicationsInput(BaseModel):
    """Input for listing applications with filters.

    Tool: greenhouse_applications_list
    API: GET /applications
    """

    per_page: int = Field(
        default=100, ge=1, le=500, description="Number of results per page (max 500)"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    skip_count: bool = Field(
        default=False, description="Skip total count calculation for performance"
    )
    created_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    created_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    last_activity_after: str | None = Field(
        default=None, description="Filter by last activity timestamp (ISO 8601)"
    )
    job_id: int | None = Field(
        default=None,
        description="Filter by job ID",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_list",
            "x-populate-field": "jobs",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    status: Literal["active", "converted", "hired", "rejected"] | None = Field(
        default=None, description="Filter by application status"
    )

    candidate_id: int | None = Field(
        default=None,
        description="Filter by candidate ID",
        json_schema_extra={
            "x-populate-from": "greenhouse_candidates_search",
            "x-populate-field": "candidates",
            "x-populate-value": "id",
            "x-populate-display": "{first_name} {last_name}",
        },
    )
    current_stage_id: int | None = Field(default=None, description="Filter by current stage ID")


class AnswerInput(BaseModel):
    """Input for a single application question answer.

    Used when submitting answers to job application questions.
    """

    question: str = Field(..., description="The question text being answered")
    answer: str | None = Field(default=None, description="The answer to the question")


class ReferrerInput(BaseModel):
    """Referrer information for an application.

    Specifies who referred the candidate. Can be:
    - An existing user (type="id", value=user_id)
    - An email address (type="email", value=email@example.com)
    - An external name (type="outside", value="John Smith")
    """

    type: Literal["id", "email", "outside"] = Field(
        ...,
        description="Type: 'id' for user ID, 'email' for email, 'outside' for external name",
    )
    value: str = Field(
        ...,
        description="The referrer value: user ID, email address, or name depending on type",
    )


class AttachmentInput(BaseModel):
    """Attachment to include with an application.

    Used for resumes, cover letters, and other documents.
    """

    filename: str = Field(..., description="Name of the file (e.g., 'resume.pdf')")
    type: Literal[
        "resume", "cover_letter", "admin_only", "take_home_test", "offer_packet", "other"
    ] = Field(default="resume", description="Type of attachment")
    content: str = Field(
        ...,
        description="Base64-encoded file content",
    )
    content_type: str | None = Field(
        default=None,
        description="MIME type (e.g., 'application/pdf'). Auto-detected if not provided.",
    )


class CreateApplicationInput(BaseModel):
    """Input for creating a new application.

    Tool: greenhouse_applications_create
    API: POST /candidates/{id}/applications

    Creates an application for an existing candidate.
    """

    candidate_id: int = Field(
        ...,
        description="Candidate ID to create application for",
        json_schema_extra={
            "x-populate-from": "greenhouse_candidates_search",
            "x-populate-field": "candidates",
            "x-populate-value": "id",
            "x-populate-display": "{first_name} {last_name}",
        },
    )
    job_id: int = Field(
        ...,
        description="Job ID to apply for",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_list",
            "x-populate-field": "jobs",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    source_id: int | None = Field(
        default=None,
        description="Source ID for attribution",
        json_schema_extra={
            "x-populate-from": "greenhouse_sources_list",
            "x-populate-field": "sources",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    initial_stage_id: int | None = Field(
        default=None,
        description="Initial pipeline stage (defaults to first stage)",
        json_schema_extra={
            "x-populate-from": "greenhouse_jobs_get_stages",
            "x-populate-field": "stages",
            "x-populate-value": "id",
            "x-populate-display": "name",
            "x-populate-dependencies": {"job_id": "job_id"},
        },
    )
    referrer: ReferrerInput | None = Field(
        default=None,
        description="Who referred this candidate (employee, email, or external)",
    )
    attachments: list[AttachmentInput] | None = Field(
        default=None,
        description="Resume, cover letter, or other documents to attach",
    )
    recruiter_id: int | None = Field(
        default=None,
        description="User ID of assigned recruiter",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    coordinator_id: int | None = Field(
        default=None,
        description="User ID of assigned coordinator",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    answers: list[AnswerInput] | None = Field(
        default=None,
        description="Answers to job application questions",
    )


class AdvanceApplicationInput(BaseModel):
    """Input for advancing an application to the next pipeline stage.

    Tool: greenhouse_applications_advance_stage
    API: POST /applications/{id}/advance

    Note: If to_stage_id is not provided, auto-advances to the next stage
    by priority. If to_stage_id is provided, moves to that specific stage
    (can skip stages).
    """

    application_id: int = Field(..., description="Application ID to advance")
    from_stage_id: int | None = Field(
        default=None, description="Current stage ID for validation (prevents race conditions)"
    )
    to_stage_id: int | None = Field(
        default=None, description="Target stage ID (auto-advance to next stage if omitted)"
    )


class MoveApplicationInput(BaseModel):
    """Input for moving an application to a specific pipeline stage.

    Tool: greenhouse_applications_move (if implemented)
    API: POST /applications/{id}/move

    Note: This allows moving to any stage, including backwards.
    """

    application_id: int = Field(..., description="Application ID to move")
    from_stage_id: int | None = Field(
        default=None, description="Current stage ID (optional validation)"
    )
    to_stage_id: int = Field(..., description="Target stage ID to move to")


class RejectApplicationInput(BaseModel):
    """Input for rejecting an application.

    Tool: greenhouse_applications_reject
    API: POST /applications/{id}/reject
    """

    application_id: int = Field(..., description="Application ID to reject")
    rejection_reason_id: int | None = Field(
        default=None,
        description="ID of the rejection reason",
        json_schema_extra={
            "x-populate-from": "greenhouse_rejection_reasons_list",
            "x-populate-field": "rejection_reasons",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    notes: str | None = Field(default=None, description="Rejection notes (added to activity feed)")
    rejection_email: dict | None = Field(
        default=None,
        description='Email settings: {"send_email_at": "ISO8601"} to schedule rejection email',
    )


class HireApplicationInput(BaseModel):
    """Input for marking an application as hired.

    Tool: greenhouse_applications_hire
    API: POST /applications/{id}/hire
    """

    application_id: int = Field(..., description="Application ID to mark as hired")
    start_date: str | None = Field(default=None, description="Hire start date (ISO 8601)")
    opening_id: int | None = Field(
        default=None, description="Job opening ID to fill with this hire"
    )
    close_reason_id: int | None = Field(default=None, description="Reason for closing the opening")
