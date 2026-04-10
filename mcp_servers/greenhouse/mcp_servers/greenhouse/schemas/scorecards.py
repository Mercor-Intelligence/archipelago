"""Scorecard/feedback-related Pydantic models for Greenhouse MCP Server."""

from __future__ import annotations

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field
from schemas.common import (
    ScorecardAttribute,
    ScorecardQuestion,
    ScorecardRatingValue,
)

RATING_BUCKETS: tuple[ScorecardRatingValue, ...] = (
    "definitely_not",
    "no",
    "mixed",
    "yes",
    "strong_yes",
    "no_decision",
)


# =============================================================================
# Input Models
# =============================================================================


class SubmitFeedbackInput(BaseModel):
    """Input for submitting interview feedback (scorecard)."""

    application_id: int = Field(
        ...,
        description="Application ID the feedback is for",
        json_schema_extra={
            "x-populate-from": "greenhouse_applications_list",
            "x-populate-field": "applications",
            "x-populate-value": "id",
            "x-populate-display": "id",
        },
    )
    interviewer_id: int = Field(
        ...,
        description="User ID of the interviewer",
        json_schema_extra={
            "x-populate-from": "greenhouse_users_list",
            "x-populate-field": "users",
            "x-populate-value": "id",
            "x-populate-display": "name",
        },
    )
    interview_step_id: int | None = Field(
        default=None, description="Interview step ID from job pipeline"
    )
    overall_recommendation: ScorecardRatingValue = Field(
        ..., description="Overall hiring recommendation"
    )
    interviewed_at: str | None = Field(
        default=None, description="When the interview took place (ISO 8601)"
    )
    attributes: list[ScorecardAttribute] | None = Field(
        default=None, description="Attribute ratings for specific skills/qualifications"
    )
    questions: list[ScorecardQuestion] | None = Field(
        default=None, description="Interview questions and answers"
    )


class ListFeedbackInput(BaseModel):
    """Input for listing scorecards for an application."""

    application_id: int = Field(
        ...,
        description="Application ID to get scorecards for",
        json_schema_extra={
            "x-populate-from": "greenhouse_applications_list",
            "x-populate-field": "applications",
            "x-populate-value": "id",
            "x-populate-display": "id",
        },
    )
    per_page: int = Field(
        default=100, ge=1, le=500, description="Number of results per page (max 500)"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")


# =============================================================================
# Output Models
# =============================================================================


class ScorecardUserOutput(BaseModel):
    """Interviewer/submitted_by user info returned in feedback responses."""

    id: int
    first_name: str | None
    last_name: str | None
    name: str
    employee_id: str | None


class ScorecardInterviewStepOutput(BaseModel):
    """Minimal interview step info included in feedback responses."""

    id: int
    name: str


class ScorecardAttributeOutput(BaseModel):
    """Auditable attribute rating returned in a scorecard."""

    name: str
    type: str | None
    rating: ScorecardRatingValue | None
    note: str | None


class ScorecardQuestionOutput(BaseModel):
    """Interview question/answer returned alongside a scorecard."""

    id: int | None = None
    question: str | None = None
    answer: str | None = None


class ScorecardRatingsOutput(BaseModel):
    """Grouped summary of attribute names per rating bucket."""

    definitely_not: list[str] = Field(default_factory=list)
    no: list[str] = Field(default_factory=list)
    mixed: list[str] = Field(default_factory=list)
    yes: list[str] = Field(default_factory=list)
    strong_yes: list[str] = Field(default_factory=list)
    no_decision: list[str] = Field(default_factory=list)


class ScorecardOutput(BaseModel):
    """Harvest-style scorecard representation returned by feedback_list."""

    id: int
    updated_at: str | None
    created_at: str | None
    interview: str | None
    interview_step: ScorecardInterviewStepOutput | None
    candidate_id: int
    application_id: int
    interviewed_at: str | None
    submitted_by: ScorecardUserOutput | None
    interviewer: ScorecardUserOutput | None
    submitted_at: str | None
    overall_recommendation: ScorecardRatingValue | None
    attributes: list[ScorecardAttributeOutput]
    ratings: ScorecardRatingsOutput
    questions: list[ScorecardQuestionOutput]


class ListFeedbackOutput(BaseModel):
    """Response payload for greenhouse_feedback_list."""

    scorecards: list[ScorecardOutput]
