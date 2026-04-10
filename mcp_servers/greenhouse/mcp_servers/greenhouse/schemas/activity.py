"""Activity feed-related Pydantic models for Greenhouse MCP Server.

Input schemas for activity tools:
- greenhouse_activity_get
"""

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field

# =============================================================================
# Input Models
# =============================================================================


class GetActivityFeedInput(BaseModel):
    """Input for retrieving a candidate's activity feed.

    Tool: greenhouse_activity_get
    API: GET /candidates/{id}/activity_feed

    Returns notes, emails, and activities for the candidate.
    """

    candidate_id: int = Field(
        ...,
        description="Candidate ID to get activity feed for",
        json_schema_extra={
            "x-populate-from": "greenhouse_candidates_search",
            "x-populate-field": "candidates",
            "x-populate-value": "id",
            "x-populate-display": "{first_name} {last_name}",
        },
    )


# =============================================================================
# Output Models
# =============================================================================


class ActivityUserOutput(BaseModel):
    """User information for activity feed items."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    employee_id: str | None = None


class ActivityNoteOutput(BaseModel):
    """Note item in activity feed."""

    id: int
    created_at: str
    body: str | None = None
    user: ActivityUserOutput | None = None
    private: bool = False
    visibility: str = "public"


class ActivityEmailOutput(BaseModel):
    """Email item in activity feed."""

    id: int
    created_at: str
    subject: str | None = None
    body: str | None = None
    to: str | None = Field(None, alias="to")
    from_: str | None = Field(None, alias="from")
    cc: str | None = None
    user: ActivityUserOutput | None = None

    model_config = {"populate_by_name": True}


class ActivityItemOutput(BaseModel):
    """System activity item in activity feed."""

    id: int
    created_at: str
    subject: str | None = None
    body: str | None = None
    user: ActivityUserOutput | None = None


class ActivityFeedOutput(BaseModel):
    """Output for greenhouse_activity_get tool."""

    notes: list[ActivityNoteOutput] = Field(default_factory=list)
    emails: list[ActivityEmailOutput] = Field(default_factory=list)
    activities: list[ActivityItemOutput] = Field(default_factory=list)
