"""Timeline tool schemas (3 tools)."""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, field_validator
from validators.business_rules import VALID_EVENT_TYPES, EventTypeLiteral, ensure_enum

from .base import PaginationRequest
from .case_schemas import GetCaseResponse


class AddTimelineEventRequest(BaseModel):
    case_id: str = Field(..., min_length=1)
    event_type: EventTypeLiteral = Field(
        ...,
        description=(
            "Event type (case_created, status_changed, owner_reassigned, due_date_updated, "
            "message_added, attachment_added, decision_logged)"
        ),
    )
    actor: str = Field(..., min_length=1)
    notes: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("event_type", mode="before")
    @classmethod
    def validate_event_type(cls, value: str | EventTypeLiteral) -> str:
        return ensure_enum(value, VALID_EVENT_TYPES, "E_VAL_001")


class AddTimelineEventResponse(BaseModel):
    event_id: str
    case_id: str
    event_type: str
    actor: str
    created_at: str
    notes: str | None
    metadata: dict[str, Any] | None


class GetTimelineEventsRequest(PaginationRequest):
    case_id: str = Field(..., min_length=1)


class GetTimelineEventsResponse(BaseModel):
    events: list[AddTimelineEventResponse]
    next_cursor: str | None
    has_more: bool
    limit: int


class GetTimelineSnapshotRequest(BaseModel):
    case_id: str = Field(..., min_length=1)
    as_of_date: str | None = Field(None, description="ISO 8601 cutoff")


class TimelineSnapshotEvent(BaseModel):
    event_id: str
    event_type: str
    timestamp: str
    actor: str | None = None
    notes: str | None = None


class MessageSnapshotEntry(BaseModel):
    message_id: str
    direction: str
    audience: str | None
    content: str
    timestamp: str


class AttachmentSnapshotEntry(BaseModel):
    attachment_id: str
    filename: str
    type: str | None
    source: str | None
    uploaded_by: str | None
    timestamp: str


class GetTimelineSnapshotResponse(BaseModel):
    case: GetCaseResponse
    timeline: list[TimelineSnapshotEvent]
    messages: list[MessageSnapshotEntry]
    attachments: list[AttachmentSnapshotEntry]
    as_of_date: str | None
