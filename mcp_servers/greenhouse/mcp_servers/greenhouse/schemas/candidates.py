"""Candidate-related Pydantic models for Greenhouse MCP Server.

Input and output schemas for candidate tools:
- greenhouse_candidates_get
- greenhouse_candidates_search
- greenhouse_candidates_create
- greenhouse_candidates_update
- greenhouse_candidates_add_note
- greenhouse_candidates_add_tag
"""

from typing import Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field
from schemas.common import (
    Address,
    Education,
    EmailAddress,
    Employment,
    PaginationMeta,
    PhoneNumber,
    SocialMediaAddress,
    WebsiteAddress,
)

# =============================================================================
# Output Models
# =============================================================================


class CandidateUserOutput(BaseModel):
    """User info for recruiter/coordinator in candidate response."""

    id: int
    first_name: str | None
    last_name: str | None
    name: str | None
    employee_id: str | None


class CandidateCurrentStageOutput(BaseModel):
    """Current stage info in application response."""

    id: int
    name: str


class CandidateJobOutput(BaseModel):
    """Job info in application response."""

    id: int
    name: str


class CandidateApplicationOutput(BaseModel):
    """Application info in candidate response."""

    id: int
    candidate_id: int
    prospect: bool
    applied_at: str | None
    status: str
    current_stage: CandidateCurrentStageOutput | None
    jobs: list[CandidateJobOutput]


class CandidateEducationOutput(BaseModel):
    """Education entry in candidate response."""

    id: int
    school_name: str | None
    degree: str | None
    discipline: str | None
    start_date: str | None
    end_date: str | None


class CandidateEmploymentOutput(BaseModel):
    """Employment entry in candidate response."""

    id: int
    company_name: str | None
    title: str | None
    start_date: str | None
    end_date: str | None


class CandidatePhoneNumberOutput(BaseModel):
    """Phone number in candidate response."""

    value: str
    type: str | None


class CandidateEmailAddressOutput(BaseModel):
    """Email address in candidate response."""

    value: str
    type: str | None


class CandidateAddressOutput(BaseModel):
    """Physical address in candidate response."""

    value: str
    type: str | None


class CandidateWebsiteAddressOutput(BaseModel):
    """Website address in candidate response."""

    value: str
    type: str | None


class CandidateSocialMediaAddressOutput(BaseModel):
    """Social media address in candidate response."""

    value: str


class CandidateOutput(BaseModel):
    """Complete candidate profile response.

    Tool: greenhouse_candidates_get
    API: GET /candidates/{id}
    """

    id: int
    first_name: str
    last_name: str
    company: str | None
    title: str | None
    created_at: str | None
    updated_at: str | None
    last_activity: str | None
    is_private: bool
    photo_url: str | None
    application_ids: list[int]
    phone_numbers: list[CandidatePhoneNumberOutput]
    addresses: list[CandidateAddressOutput]
    email_addresses: list[CandidateEmailAddressOutput]
    website_addresses: list[CandidateWebsiteAddressOutput]
    social_media_addresses: list[CandidateSocialMediaAddressOutput]
    recruiter: CandidateUserOutput | None
    coordinator: CandidateUserOutput | None
    can_email: bool
    tags: list[str]
    applications: list[CandidateApplicationOutput]
    educations: list[CandidateEducationOutput]
    employments: list[CandidateEmploymentOutput]


class CandidateNoteUserOutput(BaseModel):
    """User info attached to candidate notes."""

    id: int
    first_name: str | None
    last_name: str | None
    name: str | None
    employee_id: str | None


class CandidateNoteOutput(BaseModel):
    """Response payload for candidate notes."""

    id: int
    created_at: str | None
    body: str
    user: CandidateNoteUserOutput | None
    private: bool
    visibility: Literal["admin_only", "private", "public"]


class CandidateSearchResultOutput(BaseModel):
    """Simplified candidate representation for search results.

    Tool: greenhouse_candidates_search
    API: GET /candidates (list endpoint)
    """

    id: int
    first_name: str
    last_name: str
    company: str | None
    title: str | None
    created_at: str | None
    updated_at: str | None
    last_activity: str | None
    is_private: bool
    application_ids: list[int]
    email_addresses: list[CandidateEmailAddressOutput]
    tags: list[str]


class SearchCandidatesOutput(BaseModel):
    """Response payload for greenhouse_candidates_search."""

    candidates: list[CandidateSearchResultOutput]
    meta: PaginationMeta


# =============================================================================
# Input Models
# =============================================================================


class GetCandidateInput(BaseModel):
    """Input for retrieving a single candidate.

    Tool: greenhouse_candidates_get
    API: GET /candidates/{id}
    """

    candidate_id: int = Field(
        ...,
        description="The candidate's unique ID",
        json_schema_extra={
            "x-populate-from": "greenhouse_candidates_search",
            "x-populate-field": "candidates",
            "x-populate-value": "id",
            "x-populate-display": "{first_name} {last_name}",
        },
    )


class SearchCandidatesInput(BaseModel):
    """Input for searching/filtering candidates.

    Tool: greenhouse_candidates_search
    API: GET /candidates
    """

    per_page: int = Field(
        default=100, ge=1, le=500, description="Number of results per page (max 500)"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    skip_count: bool = Field(
        default=False, description="Skip total count calculation for performance"
    )
    name: str | None = Field(
        default=None,
        description="Search by first or last name (case-insensitive, partial match)",
    )
    email: str | None = Field(default=None, description="Filter by email address (partial match)")
    job_id: int | None = Field(
        default=None, description="Filter by job ID (candidates with applications to this job)"
    )
    tag: str | None = Field(default=None, description="Filter by tag name (exact match)")
    created_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    created_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    candidate_ids: str | None = Field(
        default=None, description="Comma-separated list of candidate IDs"
    )


class CreateCandidateInput(BaseModel):
    """Input for creating a new candidate.

    Tool: greenhouse_candidates_create
    API: POST /candidates

    Note: At least one email address is required.
    """

    first_name: str = Field(..., description="Candidate's first name")
    last_name: str = Field(..., description="Candidate's last name")
    company: str | None = Field(default=None, description="Current company")
    title: str | None = Field(default=None, description="Current job title")
    is_private: bool = Field(default=False, description="Mark candidate as private")
    phone_numbers: list[PhoneNumber] | None = Field(
        default=None, description="List of phone numbers"
    )
    addresses: list[Address] | None = Field(default=None, description="List of addresses")
    email_addresses: list[EmailAddress] = Field(
        ..., min_length=1, description="At least one email address is required"
    )
    website_addresses: list[WebsiteAddress] | None = Field(
        default=None, description="List of website URLs"
    )
    social_media_addresses: list[SocialMediaAddress] | None = Field(
        default=None, description="List of social media profiles"
    )
    tags: list[str] | None = Field(default=None, description="Tags to apply to candidate")
    educations: list[Education] | None = Field(
        default=None, description="Education history entries"
    )
    employments: list[Employment] | None = Field(
        default=None, description="Employment history entries"
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
    user_id: int | None = Field(
        default=None,
        description="User ID of creator for audit trail",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )


class UpdateCandidateInput(BaseModel):
    """Input for updating an existing candidate.

    Tool: greenhouse_candidates_update
    API: PATCH /candidates/{id}

    All fields except candidate_id are optional.
    """

    candidate_id: int = Field(
        ...,
        description="Candidate ID to update",
        json_schema_extra={
            "x-populate-from": "greenhouse_candidates_search",
            "x-populate-field": "candidates",
            "x-populate-value": "id",
            "x-populate-display": "{first_name} {last_name}",
        },
    )
    first_name: str | None = Field(default=None, description="Updated first name")
    last_name: str | None = Field(default=None, description="Updated last name")
    company: str | None = Field(default=None, description="Updated company")
    title: str | None = Field(default=None, description="Updated job title")
    is_private: bool | None = Field(default=None, description="Updated privacy setting")
    phone_numbers: list[PhoneNumber] | None = Field(
        default=None, description="Updated phone numbers (replaces existing)"
    )
    addresses: list[Address] | None = Field(
        default=None, description="Updated addresses (replaces existing)"
    )
    email_addresses: list[EmailAddress] | None = Field(
        default=None, description="Updated email addresses (replaces existing)"
    )
    website_addresses: list[WebsiteAddress] | None = Field(
        default=None, description="Updated website URLs (replaces existing)"
    )
    social_media_addresses: list[SocialMediaAddress] | None = Field(
        default=None, description="Updated social media profiles (replaces existing)"
    )
    tags: list[str] | None = Field(default=None, description="Updated tags (replaces existing)")
    recruiter_id: int | None = Field(
        default=None,
        description="Updated recruiter assignment",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    coordinator_id: int | None = Field(
        default=None,
        description="Updated coordinator assignment",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )


class AddCandidateNoteInput(BaseModel):
    """Input for adding a note to a candidate.

    Tool: greenhouse_candidates_add_note
    API: POST /candidates/{id}/activity_feed/notes
    """

    candidate_id: int = Field(
        ...,
        description="Candidate ID to add note to",
        json_schema_extra={
            "x-populate-from": "greenhouse_candidates_search",
            "x-populate-field": "candidates",
            "x-populate-value": "id",
            "x-populate-display": "{first_name} {last_name}",
        },
    )
    user_id: int = Field(
        ...,
        description="User ID of note author",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    body: str = Field(..., description="Note content")
    visibility: Literal["admin_only", "private", "public"] = Field(
        default="public",
        description="Note visibility: admin_only, private, or public",
    )


class AddCandidateTagInput(BaseModel):
    """Input for adding a tag to a candidate.

    Tool: greenhouse_candidates_add_tag
    API: PUT /candidates/{id}/tags
    """

    candidate_id: int = Field(
        ...,
        description="Candidate ID to add tag to",
        json_schema_extra={
            "x-populate-from": "greenhouse_candidates_search",
            "x-populate-field": "candidates",
            "x-populate-value": "id",
            "x-populate-display": "{first_name} {last_name}",
        },
    )
    tag: str = Field(..., description="Tag name to add")


class TagOutput(BaseModel):
    """Tag information."""

    id: int = Field(..., description="Tag ID")
    name: str = Field(..., description="Tag name")


class AddCandidateTagOutput(BaseModel):
    """Output for adding a tag to a candidate.

    Tool: greenhouse_candidates_add_tag
    """

    candidate_id: int = Field(..., description="Candidate ID the tag was added to")
    tag: TagOutput = Field(..., description="The tag that was added")
