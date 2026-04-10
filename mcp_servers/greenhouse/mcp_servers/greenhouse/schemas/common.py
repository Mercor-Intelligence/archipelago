"""Common/shared Pydantic models for Greenhouse MCP Server.

These models are used across multiple tool input schemas.
"""

from typing import Annotated, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field

# =============================================================================
# Email Type Alias
# =============================================================================
# To switch back to EmailStr validation (requires email-validator package):
#   1. Uncomment: from pydantic import EmailStr
#   2. Change: Email = EmailStr
#   3. Remove the EMAIL_PATTERN and Annotated version
#
# Current: Using regex pattern to avoid email-validator/idna dependency issues

EMAIL_PATTERN = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
Email = Annotated[str, Field(pattern=EMAIL_PATTERN)]

ScorecardRatingValue = Literal[
    "definitely_not",
    "no",
    "mixed",
    "yes",
    "strong_yes",
    "no_decision",
]

# =============================================================================
# Contact Information Models
# =============================================================================


class PhoneNumber(BaseModel):
    """Phone number with type classification."""

    value: str = Field(..., description="Phone number value")
    type: Literal["home", "work", "mobile", "skype", "other"] | None = Field(
        default="mobile", description="Phone number type"
    )


class EmailAddress(BaseModel):
    """Email address with type classification."""

    value: Email = Field(..., description="Email address")
    type: Literal["personal", "work", "other"] | None = Field(
        default="personal", description="Email address type"
    )


class Address(BaseModel):
    """Physical address with type classification."""

    value: str = Field(..., description="Full address")
    type: Literal["home", "work", "other"] | None = Field(
        default="home", description="Address type"
    )


class WebsiteAddress(BaseModel):
    """Website URL with type classification."""

    value: str = Field(..., description="Website URL")
    type: Literal["personal", "company", "portfolio", "blog", "other"] | None = Field(
        default="personal", description="Website type"
    )


class SocialMediaAddress(BaseModel):
    """Social media profile URL."""

    value: str = Field(..., description="Social media URL")


# =============================================================================
# Education and Employment Models
# =============================================================================


class Education(BaseModel):
    """Education entry for candidate profile."""

    school_id: int | None = Field(default=None, description="Reference to schools table")
    school_name: str | None = Field(default=None, description="School name (if not using ID)")
    degree_id: int | None = Field(default=None, description="Reference to degrees table")
    degree: str | None = Field(default=None, description="Degree name (if not using ID)")
    discipline_id: int | None = Field(default=None, description="Reference to disciplines table")
    discipline: str | None = Field(default=None, description="Field of study (if not using ID)")
    start_date: str | None = Field(default=None, description="Start date (ISO 8601)")
    end_date: str | None = Field(default=None, description="End date (ISO 8601)")


class Employment(BaseModel):
    """Employment history entry for candidate profile."""

    company_name: str | None = Field(default=None, description="Company name")
    title: str | None = Field(default=None, description="Job title")
    start_date: str | None = Field(default=None, description="Start date (ISO 8601)")
    end_date: str | None = Field(default=None, description="End date (ISO 8601)")


# =============================================================================
# Scorecard/Feedback Models
# =============================================================================


class ScorecardAttribute(BaseModel):
    """Scorecard attribute rating for interview feedback."""

    name: str = Field(..., description="Attribute name (e.g., 'Communication')")
    type: Literal["Skills", "Qualifications"] | None = Field(
        default="Skills", description="Attribute category"
    )
    rating: ScorecardRatingValue = Field(..., description="Rating for this attribute")
    note: str | None = Field(default=None, description="Optional note explaining the rating")


class ScorecardQuestion(BaseModel):
    """Scorecard question and answer for interview feedback."""

    id: int | None = Field(default=None, description="Question ID (if from interview kit)")
    question: str = Field(..., description="The interview question")
    answer: str = Field(..., description="The interviewer's answer/notes")


# =============================================================================
# Pagination Mixin
# =============================================================================


class PaginationParams(BaseModel):
    """Common pagination parameters for list endpoints."""

    per_page: int = Field(
        default=100, ge=1, le=500, description="Number of results per page (max 500)"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    skip_count: bool = Field(
        default=False, description="Skip total count calculation for performance"
    )


class PaginationMeta(BaseModel):
    """Pagination metadata for list/search responses."""

    per_page: int = Field(..., description="Number of results per page")
    page: int = Field(..., description="Current page number (1-indexed)")
    total: int | None = Field(default=None, description="Total count of results (None if skipped)")
    links: dict[str, str | None] | None = Field(
        default=None, description="Pagination links (first, prev, self, next, last)"
    )


class TimestampFilters(BaseModel):
    """Common timestamp filter parameters."""

    created_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    created_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_before: str | None = Field(default=None, description="ISO 8601 timestamp filter")
    updated_after: str | None = Field(default=None, description="ISO 8601 timestamp filter")


# =============================================================================
# Job Board Education Input Models
# =============================================================================


class EducationInput(BaseModel):
    """Education input for job board applications."""

    school_name_id: int | None = Field(
        default=None, description="From GET /boards/{token}/education/schools"
    )
    degree_id: int | None = Field(
        default=None, description="From GET /boards/{token}/education/degrees"
    )
    discipline_id: int | None = Field(
        default=None, description="From GET /boards/{token}/education/disciplines"
    )
    start_date: dict | None = Field(
        default=None, description='Start date as {"month": "1", "year": "2020"}'
    )
    end_date: dict | None = Field(
        default=None, description='End date as {"month": "5", "year": "2024"}'
    )


class EmploymentInput(BaseModel):
    """Employment input for job board applications."""

    company_name: str = Field(..., description="Company name")
    title: str = Field(..., description="Job title")
    start_date: dict = Field(..., description='Start date as {"month": "1", "year": "2020"}')
    end_date: dict | None = Field(default=None, description="End date (if not current)")
    current: bool = Field(default=False, description="Is this the current position")
