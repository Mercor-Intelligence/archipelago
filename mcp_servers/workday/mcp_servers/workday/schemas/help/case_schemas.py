"""Case tool schemas (6 tools)."""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, field_validator
from validators.business_rules import (
    SUPPORTED_PERSONAS,
    VALID_CASE_TYPES,
    VALID_STATUSES,
    CaseTypeLiteral,
    PersonaLiteral,
    StatusLiteral,
    ensure_enum,
    ensure_future_iso8601,
    ensure_valid_transition,
)

from .base import PaginationRequest

STATUS_OPTION_TEXT = "/".join(VALID_STATUSES)


class CreateCaseRequest(BaseModel):
    case_type: CaseTypeLiteral = Field(..., description="Case type. Values: 'Pre-Onboarding'.")
    owner: str = Field(
        ..., min_length=1, description="Case owner email address (e.g., 'hr@company.com')."
    )
    case_id: str = Field(
        ...,
        min_length=1,
        description="Unique case identifier. Format: CASE-YYYYMMDD-### (e.g., 'CASE-20240115-001'). Must be unique.",
    )
    status: StatusLiteral = Field(
        ...,
        description="Initial case status. Values: 'Open', 'Waiting', 'In Progress', 'Resolved', 'Closed'.",
    )
    candidate_identifier: str = Field(
        ...,
        min_length=1,
        description="Unique candidate identifier from ATS (e.g., 'CAND-12345').",
    )
    due_date: str | None = Field(
        None,
        description="Case deadline in ISO 8601 format (e.g., '2024-03-15T00:00:00Z'). Must be a future date.",
    )
    metadata: dict[str, Any] | None = Field(
        None,
        description='Additional metadata as JSON object. Example: {"source": "ATS", "priority": "high"}.',
    )
    actor_persona: PersonaLiteral | None = Field(
        None,
        description="Persona context for access control. Values: 'case_owner', 'hr_admin', 'manager', 'hr_analyst'.",
    )
    actor: str | None = Field(
        None,
        description="Actor email/user ID for audit scope (defaults to authenticated user or owner).",
    )

    @field_validator("case_type", mode="before")
    @classmethod
    def check_case_type(cls, v: str) -> str:
        return ensure_enum(v, VALID_CASE_TYPES, "E_CASE_005")

    @field_validator("case_id")
    @classmethod
    def check_case_id(cls, v: str) -> str:
        normalized = v.strip()
        if not normalized:
            raise ValueError("E_VAL_001: case_id cannot be empty")
        return normalized

    @field_validator("status", mode="before")
    @classmethod
    def check_status(cls, v: str) -> str:
        return ensure_enum(v, VALID_STATUSES, "E_CASE_002")

    @field_validator("due_date")
    @classmethod
    def check_due_date(cls, v: str | None) -> str | None:
        return ensure_future_iso8601(v, "due_date")

    @field_validator("actor_persona", mode="before")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        return ensure_enum(v, SUPPORTED_PERSONAS, "E_AUTH_001") if v is not None else None


class CreateCaseResponse(BaseModel):
    case_id: str = Field(..., description="The created case ID.")
    case_type: str = Field(..., description="Case type.")
    owner: str = Field(..., description="Case owner email address.")
    status: str = Field(..., description="Current case status.")
    candidate_identifier: str = Field(..., description="Candidate identifier.")
    due_date: str | None = Field(None, description="Case deadline in ISO 8601 format.")
    created_at: str = Field(..., description="Record creation timestamp in ISO 8601 format.")
    updated_at: str = Field(..., description="Record last update timestamp in ISO 8601 format.")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata.")
    timeline_event_id: str = Field(
        ..., description="ID of the timeline event created for this action."
    )
    audit_log_id: str = Field(..., description="ID of the audit log entry for this action.")


class GetCaseRequest(BaseModel):
    case_id: str = Field(
        ...,
        min_length=1,
        description="Case ID to retrieve. Format: CASE-YYYYMMDD-### (e.g., 'CASE-20240115-001').",
    )
    actor_persona: PersonaLiteral | None = Field(
        None,
        description="Persona context for access control. Values: 'case_owner', 'hr_admin', 'manager', 'hr_analyst'.",
    )
    actor: str | None = Field(
        None,
        description="Actor email/user ID for scope validation (defaults to authenticated user).",
    )

    @field_validator("actor_persona", mode="before")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        return ensure_enum(v, SUPPORTED_PERSONAS, "E_AUTH_001") if v is not None else None


class GetCaseResponse(BaseModel):
    case_id: str = Field(..., description="The case ID.")
    case_type: str = Field(..., description="Case type.")
    owner: str = Field(..., description="Case owner email address.")
    status: str = Field(..., description="Current case status.")
    candidate_identifier: str = Field(..., description="Candidate identifier.")
    due_date: str | None = Field(None, description="Case deadline in ISO 8601 format.")
    created_at: str = Field(..., description="Record creation timestamp in ISO 8601 format.")
    updated_at: str = Field(..., description="Record last update timestamp in ISO 8601 format.")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata.")


class UpdateCaseStatusRequest(BaseModel):
    """Update case status with optimistic concurrency control.

    Valid status transitions:
    - Open -> Waiting, In Progress, Closed
    - Waiting -> Open, In Progress, Closed
    - In Progress -> Waiting, Resolved, Closed
    - Resolved -> Closed, In Progress (reopen)
    """

    case_id: str = Field(
        ...,
        min_length=1,
        description="Case ID to update. Format: CASE-YYYYMMDD-### (e.g., 'CASE-20240115-001').",
    )
    current_status: StatusLiteral = Field(
        ...,
        description="Current status for optimistic concurrency validation. Values: 'Open', 'Waiting', 'In Progress', 'Resolved', 'Closed'.",
    )
    new_status: StatusLiteral = Field(
        ...,
        description="New status to transition to. Values: 'Open', 'Waiting', 'In Progress', 'Resolved', 'Closed'. Must be a valid transition.",
    )
    rationale: str = Field(
        ..., min_length=1, description="Reason for status change. Required for audit trail."
    )
    actor_persona: PersonaLiteral | None = Field(
        None,
        description="Persona context for access control. Values: 'case_owner', 'hr_admin', 'manager', 'hr_analyst'.",
    )
    actor: str | None = Field(
        None,
        description="Actor email/user ID for audit scope (defaults to authenticated user).",
    )

    @field_validator("current_status", "new_status", mode="before")
    @classmethod
    def check_status(cls, v: str) -> str:
        return ensure_enum(v, VALID_STATUSES, "E_CASE_002")

    @field_validator("new_status")
    @classmethod
    def check_transition(cls, v: str, values):
        current = None
        if hasattr(values, "data"):
            current = values.data.get("current_status")
        elif isinstance(values, dict):
            current = values.get("current_status")
        if current:
            ensure_valid_transition(current, v)
        return v

    @field_validator("actor_persona", mode="before")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        return ensure_enum(v, SUPPORTED_PERSONAS, "E_AUTH_001") if v is not None else None


class UpdateCaseStatusResponse(BaseModel):
    case_id: str = Field(..., description="The updated case ID.")
    previous_status: str = Field(..., description="The status before the update.")
    new_status: str = Field(..., description="The new status after the update.")
    updated_at: str = Field(..., description="Record update timestamp in ISO 8601 format.")
    timeline_event_id: str = Field(
        ..., description="ID of the timeline event created for this action."
    )
    audit_log_id: str = Field(..., description="ID of the audit log entry for this action.")


class ReassignCaseOwnerRequest(BaseModel):
    case_id: str = Field(
        ...,
        min_length=1,
        description="Case ID to reassign. Format: CASE-YYYYMMDD-### (e.g., 'CASE-20240115-001').",
    )
    new_owner: str = Field(
        ...,
        min_length=1,
        description="New owner email address (e.g., 'newowner@company.com').",
    )
    rationale: str = Field(
        ..., min_length=1, description="Reason for reassignment. Required for audit trail."
    )
    actor_persona: PersonaLiteral | None = Field(
        None,
        description="Persona context for access control. Values: 'case_owner', 'hr_admin', 'manager', 'hr_analyst'.",
    )
    actor: str | None = Field(
        None,
        description="Actor email/user ID for audit scope (defaults to authenticated user).",
    )

    @field_validator("actor_persona", mode="before")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        return ensure_enum(v, SUPPORTED_PERSONAS, "E_AUTH_001") if v is not None else None


class ReassignCaseOwnerResponse(BaseModel):
    case_id: str = Field(..., description="The updated case ID.")
    previous_owner: str = Field(..., description="The previous owner email address.")
    new_owner: str = Field(..., description="The new owner email address.")
    updated_at: str = Field(..., description="Record update timestamp in ISO 8601 format.")
    timeline_event_id: str = Field(
        ..., description="ID of the timeline event created for this action."
    )
    audit_log_id: str = Field(..., description="ID of the audit log entry for this action.")


class UpdateCaseDueDateRequest(BaseModel):
    case_id: str = Field(
        ...,
        min_length=1,
        description="Case ID to update. Format: CASE-YYYYMMDD-### (e.g., 'CASE-20240115-001').",
    )
    new_due_date: str = Field(
        ...,
        description="New due date in ISO 8601 format (e.g., '2024-03-15T00:00:00Z'). Must be a future date.",
    )
    rationale: str = Field(
        ..., min_length=1, description="Reason for due date change. Required for audit trail."
    )
    actor_persona: PersonaLiteral | None = Field(
        None,
        description="Persona context for access control. Values: 'case_owner', 'hr_admin', 'manager', 'hr_analyst'.",
    )
    actor: str | None = Field(
        None,
        description="Actor email/user ID for audit scope (defaults to authenticated user).",
    )

    @field_validator("new_due_date")
    @classmethod
    def check_due_date(cls, v: str) -> str:
        return ensure_future_iso8601(v, "new_due_date") or v

    @field_validator("actor_persona", mode="before")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        return ensure_enum(v, SUPPORTED_PERSONAS, "E_AUTH_001") if v is not None else None


class UpdateCaseDueDateResponse(BaseModel):
    case_id: str = Field(..., description="The updated case ID.")
    previous_due_date: str | None = Field(
        None, description="The previous due date in ISO 8601 format."
    )
    new_due_date: str = Field(..., description="The new due date in ISO 8601 format.")
    updated_at: str = Field(..., description="Record update timestamp in ISO 8601 format.")
    timeline_event_id: str = Field(
        ..., description="ID of the timeline event created for this action."
    )
    audit_log_id: str = Field(..., description="ID of the audit log entry for this action.")


class SearchCasesRequest(PaginationRequest):
    status: list[StatusLiteral] | None = Field(
        None,
        description="Filter by status(es). Values: 'Open', 'Waiting', 'In Progress', 'Resolved', 'Closed'. Accepts single value or list.",
    )
    owner: str | None = Field(
        None, description="Filter by owner email address (e.g., 'hr@company.com')."
    )
    candidate_identifier: str | None = Field(
        None, description="Filter by candidate identifier (e.g., 'CAND-12345')."
    )
    created_after: str | None = Field(
        None, description="Filter for cases created after this date in ISO 8601 format."
    )
    created_before: str | None = Field(
        None, description="Filter for cases created before this date in ISO 8601 format."
    )
    actor_persona: PersonaLiteral | None = Field(
        None,
        description="Persona context for access control. Values: 'case_owner', 'hr_admin', 'manager', 'hr_analyst'.",
    )
    actor: str | None = Field(
        None,
        description="Actor email/user ID for scope validation (defaults to authenticated user).",
    )

    @field_validator("status", mode="before")
    @classmethod
    def normalize_and_check_status(cls, v: str | list[str] | None) -> list[str] | None:
        if v is None:
            return None
        # Normalize to list
        if isinstance(v, str):
            v = [v]
        else:
            v = list(v)
        # Validate each status value
        return [ensure_enum(status, VALID_STATUSES, "E_CASE_002") for status in v]

    @field_validator("created_before")
    @classmethod
    def validate_range(cls, v: str | None, values):
        values_data = None
        if hasattr(values, "data"):
            values_data = values.data
        elif isinstance(values, dict):
            values_data = values

        created_after = values_data.get("created_after") if values_data else None
        from validators.business_rules import validate_date_range

        validate_date_range(created_after, v, "created_at")
        return v

    @field_validator("actor_persona", mode="before")
    @classmethod
    def check_actor_persona(cls, v: str | None) -> str | None:
        return ensure_enum(v, SUPPORTED_PERSONAS, "E_AUTH_001") if v is not None else None


class CaseSummary(BaseModel):
    case_id: str = Field(..., description="The case ID.")
    case_type: str = Field(..., description="Case type.")
    owner: str = Field(..., description="Case owner email address.")
    status: str = Field(..., description="Current case status.")
    candidate_identifier: str = Field(..., description="Candidate identifier.")
    due_date: str | None = Field(None, description="Case deadline in ISO 8601 format.")
    created_at: str = Field(..., description="Record creation timestamp in ISO 8601 format.")
    updated_at: str = Field(..., description="Record last update timestamp in ISO 8601 format.")


class SearchCasesResponse(BaseModel):
    cases: list[CaseSummary] = Field(..., description="List of cases matching the search criteria.")
    next_cursor: str | None = Field(
        None, description="Pagination cursor for next page. Null if no more results."
    )
    has_more: bool = Field(..., description="True if there are more results beyond this page.")
    limit: int = Field(..., description="Number of results returned in this page.")
