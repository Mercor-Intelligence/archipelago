"""Attachment tool schemas (2 tools)."""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, field_validator
from validators.business_rules import SUPPORTED_PERSONAS

from .base import PaginationRequest


class AddAttachmentRequest(BaseModel):
    case_id: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    mime_type: str | None = None
    source: str | None = None
    external_reference: str | None = None
    size_bytes: int | None = Field(None, ge=0)
    uploader: str = Field(..., min_length=1)
    metadata: dict[str, Any] | None = None
    actor_persona: str | None = Field(
        None,
        description="Persona context (case_owner, hr_admin, manager, hr_analyst)",
    )

    @field_validator("actor_persona")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in SUPPORTED_PERSONAS:
            raise ValueError("E_AUTH_001: Invalid persona")
        return v


class AddAttachmentResponse(BaseModel):
    attachment_id: str
    case_id: str
    filename: str
    mime_type: str | None
    source: str | None
    external_reference: str | None
    size_bytes: int | None
    uploader: str
    uploaded_at: str
    metadata: dict[str, Any] | None


class ListAttachmentsRequest(PaginationRequest):
    case_id: str = Field(..., min_length=1)
    actor_persona: str | None = Field(
        None,
        description="Persona context (case_owner, hr_admin, manager, hr_analyst)",
    )

    @field_validator("actor_persona")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in SUPPORTED_PERSONAS:
            raise ValueError("E_AUTH_001: Invalid persona")
        return v


class AttachmentSummary(BaseModel):
    attachment_id: str
    case_id: str
    filename: str
    mime_type: str | None
    source: str | None
    external_reference: str | None
    size_bytes: int | None
    uploader: str
    uploaded_at: str
    metadata: dict[str, Any] | None


class ListAttachmentsResponse(BaseModel):
    attachments: list[AttachmentSummary]
    next_cursor: str | None
    has_more: bool
    limit: int
