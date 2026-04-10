"""Message tool schemas (3 tools)."""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, field_validator, model_validator
from validators.business_rules import (
    SUPPORTED_PERSONAS,
    VALID_AUDIENCES,
    VALID_DIRECTIONS,
    AudienceLiteral,
    DirectionLiteral,
    PersonaLiteral,
    ensure_enum,
)

from .base import PaginationRequest


class AddMessageRequest(BaseModel):
    case_id: str = Field(..., min_length=1)
    direction: DirectionLiteral = Field(..., description="Message direction")
    sender: str = Field(..., min_length=1, description="Message sender/author")
    body: str = Field(..., min_length=1)
    actor: str = Field(
        ...,
        min_length=1,
        description="System user logging this message (may differ from sender for inbound)",
    )
    actor_persona: PersonaLiteral | None = Field(
        None,
        description="Persona context",
    )
    audience: AudienceLiteral | None = Field(
        None, description="Target audience (required for inbound/outbound)"
    )
    metadata: dict[str, Any] | None = None

    @field_validator("direction", mode="before")
    @classmethod
    def check_direction(cls, v: str) -> str:
        return ensure_enum(v, VALID_DIRECTIONS)

    @field_validator("audience", mode="before")
    @classmethod
    def check_audience(cls, v: str | None) -> str | None:
        return ensure_enum(v, VALID_AUDIENCES) if v is not None else v

    @model_validator(mode="after")
    def require_audience_for_external(self) -> "AddMessageRequest":
        if self.direction in ("inbound", "outbound") and not self.audience:
            raise ValueError("E_VAL_001: audience is required for inbound/outbound messages")
        return self

    @field_validator("actor_persona", mode="before")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        return ensure_enum(v, SUPPORTED_PERSONAS, "E_AUTH_001") if v is not None else None


class AddMessageResponse(BaseModel):
    message_id: str
    case_id: str
    direction: str
    sender: str
    audience: str | None
    body: str
    created_at: str
    metadata: dict[str, Any] | None


class GetMessageRequest(BaseModel):
    message_id: str = Field(..., min_length=1)


class GetMessageResponse(BaseModel):
    message_id: str
    case_id: str
    direction: str
    sender: str
    audience: str | None
    body: str
    created_at: str
    metadata: dict[str, Any] | None


class SearchMessagesRequest(PaginationRequest):
    message_id: str | None = Field(
        None,
        min_length=1,
        description="Message ID filter (exact match)",
    )
    case_id: str | None = None
    direction: DirectionLiteral | None = Field(None, description="Filter by message direction")
    sender: str | None = None
    created_after: str | None = None
    created_before: str | None = None

    @field_validator("direction", mode="before")
    @classmethod
    def check_direction(cls, v: str | None) -> str | None:
        return ensure_enum(v, VALID_DIRECTIONS) if v is not None else v

    @field_validator("created_before")
    @classmethod
    def check_date_range(cls, v: str | None, values):
        from validators.business_rules import validate_date_range

        start = None
        if hasattr(values, "data"):
            start = values.data.get("created_after")
        elif isinstance(values, dict):
            start = values.get("created_after")
        validate_date_range(start, v, "created_at")
        return v


class SearchMessagesResponse(BaseModel):
    messages: list[GetMessageResponse]
    next_cursor: str | None
    has_more: bool
    limit: int
