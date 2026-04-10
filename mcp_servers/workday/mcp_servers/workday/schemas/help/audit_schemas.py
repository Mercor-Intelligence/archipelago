"""Audit tool schema (1 tool)."""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel

from .base import PaginationRequest


class QueryAuditHistoryRequest(PaginationRequest):
    case_id: str | None = None
    actor: str | None = None
    action_type: str | None = None
    created_after: str | None = None
    created_before: str | None = None


class AuditEntry(BaseModel):
    log_id: str
    case_id: str
    entity_type: str
    entity_id: str
    action: str
    actor: str
    actor_persona: str
    created_at: str
    changes: dict[str, Any] | None
    rationale: str | None
    metadata: dict[str, Any] | None


class QueryAuditHistoryResponse(BaseModel):
    audit_log: list[AuditEntry]
    next_cursor: str | None
    has_more: bool
    limit: int
