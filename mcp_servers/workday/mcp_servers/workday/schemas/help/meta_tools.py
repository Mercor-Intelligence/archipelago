"""Meta-tool Pydantic models for Workday Help MCP Server.

This module defines Input, Output, and Help models for consolidated meta-tools
that route multiple actions through a single tool interface. Meta-tools reduce
LLM context size by ~75% while maintaining full functionality.

Pattern:
- Each meta-tool has one Input model with an `action` field for routing
- Each meta-tool has one Output model with `action`, `help`, `data`, and `error` fields
- HelpResponse provides self-documentation for each meta-tool
- SchemaInput/SchemaOutput enable runtime schema introspection

Available Meta-Tools:
- workday_help_cases: Manage HR cases (create, get, update_status, reassign_owner,
  update_due_date, search)
- workday_help_messages: Manage case messages (add, get, search)
- workday_help_timeline: Manage case timeline (add_event, get_events, get_snapshot)
- workday_help_attachments: Manage case attachments (add, list)
- workday_help_audit: Query audit history
- workday_help_admin: Server admin operations (health_check, get_server_info)
- workday_help_schema: Schema introspection for all meta-tools
"""

from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field
from validators.business_rules import (
    AudienceLiteral,
    CaseTypeLiteral,
    DirectionLiteral,
    EventTypeLiteral,
    PersonaLiteral,
    StatusLiteral,
)

# =============================================================================
# Shared Models
# =============================================================================


class HelpResponse(BaseModel):
    """Standard help response for all meta-tools.

    Provides self-documentation for LLMs to discover available actions
    and their required/optional parameters.
    """

    tool_name: str = Field(..., description="Name of the meta-tool")
    description: str = Field(..., description="What this meta-tool does")
    actions: dict[str, dict[str, Any]] = Field(
        ...,
        description="Map of action name to {description, required_params, optional_params}",
    )


class MetaToolOutput(BaseModel):
    """Standard output for all meta-tools.

    Provides a consistent response structure across all meta-tools.
    """

    action: str = Field(..., description="Action that was performed")
    help: HelpResponse | None = Field(
        default=None, description="Help information (when action='help')"
    )
    data: dict[str, Any] | None = Field(default=None, description="Response data for the action")
    error: str | None = Field(default=None, description="Error message if the action failed")


# =============================================================================
# Schema Introspection Models
# =============================================================================


class SchemaInput(BaseModel):
    """Input for schema introspection tool.

    Allows LLMs to retrieve the full JSON schema for any meta-tool's
    input and output models at runtime.
    """

    model_config = ConfigDict(populate_by_name=True)

    tool: str = Field(..., description="Meta-tool name to get schema for")


class SchemaOutput(BaseModel):
    """Output for schema introspection tool."""

    tool: str = Field(..., description="Meta-tool name")
    input_schema: dict[str, Any] = Field(
        ..., alias="inputSchema", description="JSON schema for input"
    )
    output_schema: dict[str, Any] = Field(
        ..., alias="outputSchema", description="JSON schema for output"
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# Cases Meta-Tool Models
# =============================================================================


class CasesInput(BaseModel):
    """Input for the cases meta-tool.

    Consolidates create, get, update_status, reassign_owner, update_due_date,
    and search operations for HR cases.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Action selector (required)
    action: Literal[
        "help", "create", "get", "update_status", "reassign_owner", "update_due_date", "search"
    ] = Field(..., description="Action to perform")

    # --- Common parameters ---
    case_id: str | None = Field(
        default=None,
        alias="caseId",
        description="Case ID (required for get, update_status, reassign_owner, update_due_date)",
    )
    actor_persona: PersonaLiteral | None = Field(
        default=None,
        alias="actorPersona",
        description="Persona context",
    )
    actor: str | None = Field(
        default=None,
        description="Actor email/user ID for audit scope",
    )

    # --- Create action parameters ---
    case_type: CaseTypeLiteral | None = Field(
        default=None,
        alias="caseType",
        description="Case type (required for 'create')",
    )
    owner: str | None = Field(
        default=None,
        description="Case owner email (required for 'create')",
    )
    status: StatusLiteral | None = Field(
        default=None,
        description="Case status (required for 'create')",
    )
    candidate_identifier: str | None = Field(
        default=None,
        alias="candidateIdentifier",
        description="Candidate identifier (required for 'create')",
    )
    due_date: str | None = Field(
        default=None,
        alias="dueDate",
        description="Due date in ISO 8601 format",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata key-value pairs",
    )

    # --- Update status parameters ---
    current_status: StatusLiteral | None = Field(
        default=None,
        alias="currentStatus",
        description="Current status for validation (required for 'update_status')",
    )
    new_status: StatusLiteral | None = Field(
        default=None,
        alias="newStatus",
        description="New status to transition to (required for 'update_status')",
    )
    rationale: str | None = Field(
        default=None,
        description="Reason for status change/reassignment/due date update (required for updates)",
    )

    # --- Reassign owner parameters ---
    new_owner: str | None = Field(
        default=None,
        alias="newOwner",
        description="New owner email (required for 'reassign_owner')",
    )

    # --- Update due date parameters ---
    new_due_date: str | None = Field(
        default=None,
        alias="newDueDate",
        description="New due date in ISO 8601 format (required for 'update_due_date')",
    )

    # --- Search parameters ---
    statuses: list[StatusLiteral] | None = Field(
        default=None,
        description="Filter by statuses (for 'search')",
    )
    created_after: str | None = Field(
        default=None,
        alias="createdAfter",
        description="Filter: created after this ISO 8601 date",
    )
    created_before: str | None = Field(
        default=None,
        alias="createdBefore",
        description="Filter: created before this ISO 8601 date",
    )
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor for 'search'",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Results per page (max 200)",
    )


# =============================================================================
# Messages Meta-Tool Models
# =============================================================================


class MessagesInput(BaseModel):
    """Input for the messages meta-tool.

    Consolidates add, get, and search operations for case messages.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Action selector (required)
    action: Literal["help", "add", "get", "search"] = Field(..., description="Action to perform")

    # --- Common parameters ---
    case_id: str | None = Field(
        default=None,
        alias="caseId",
        description="Case ID (required for 'add', optional filter for 'search')",
    )

    # --- Get action parameters ---
    message_id: str | None = Field(
        default=None,
        alias="messageId",
        description="Message ID (required for 'get')",
    )

    # --- Add action parameters ---
    direction: DirectionLiteral | None = Field(
        default=None,
        description="Message direction (required for 'add')",
    )
    sender: str | None = Field(
        default=None,
        description="Sender email (required for 'add')",
    )
    body: str | None = Field(
        default=None,
        description="Message body content (required for 'add')",
    )
    audience: AudienceLiteral | None = Field(
        default=None,
        description="Target audience for the message",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata key-value pairs",
    )
    actor: str | None = Field(
        default=None,
        description="Actor email for audit",
    )
    actor_persona: PersonaLiteral | None = Field(
        default=None,
        alias="actorPersona",
        description="Persona context",
    )

    # --- Search parameters ---
    created_after: str | None = Field(
        default=None,
        alias="createdAfter",
        description="Filter: created after this ISO 8601 date",
    )
    created_before: str | None = Field(
        default=None,
        alias="createdBefore",
        description="Filter: created before this ISO 8601 date",
    )
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Results per page (max 200)",
    )


# =============================================================================
# Timeline Meta-Tool Models
# =============================================================================


class TimelineInput(BaseModel):
    """Input for the timeline meta-tool.

    Consolidates add_event, get_events, and get_snapshot operations for case timelines.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Action selector (required)
    action: Literal["help", "add_event", "get_events", "get_snapshot"] = Field(
        ..., description="Action to perform"
    )

    # --- Common parameters ---
    case_id: str | None = Field(
        default=None,
        alias="caseId",
        description="Case ID (required for all actions)",
    )

    # --- Add event parameters ---
    event_type: EventTypeLiteral | None = Field(
        default=None,
        alias="eventType",
        description="Event type (required for 'add_event')",
    )
    actor: str | None = Field(
        default=None,
        description="Actor who triggered the event",
    )
    notes: str | None = Field(
        default=None,
        description="Event notes/description",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata key-value pairs",
    )

    # --- Get snapshot parameters ---
    as_of_date: str | None = Field(
        default=None,
        alias="asOfDate",
        description="Point-in-time snapshot date (ISO 8601)",
    )

    # --- Pagination parameters ---
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Results per page (max 200)",
    )


# =============================================================================
# Attachments Meta-Tool Models
# =============================================================================


class AttachmentsInput(BaseModel):
    """Input for the attachments meta-tool.

    Consolidates add and list operations for case attachments.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Action selector (required)
    action: Literal["help", "add", "list"] = Field(..., description="Action to perform")

    # --- Common parameters ---
    case_id: str | None = Field(
        default=None,
        alias="caseId",
        description="Case ID (required for 'add' and 'list')",
    )
    actor_persona: PersonaLiteral | None = Field(
        default=None,
        alias="actorPersona",
        description="Persona context",
    )

    # --- Add action parameters ---
    filename: str | None = Field(
        default=None,
        description="Attachment filename (required for 'add')",
    )
    uploader: str | None = Field(
        default=None,
        description="Uploader email (required for 'add')",
    )
    mime_type: str | None = Field(
        default=None,
        alias="mimeType",
        description="MIME type of the attachment",
    )
    size_bytes: int | None = Field(
        default=None,
        alias="sizeBytes",
        description="File size in bytes",
        ge=0,
    )
    source: str | None = Field(
        default=None,
        description="Source of the attachment",
    )
    external_reference: str | None = Field(
        default=None,
        alias="externalReference",
        description="External storage reference/URL",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata key-value pairs",
    )

    # --- Pagination parameters ---
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Results per page (max 200)",
    )


# =============================================================================
# Audit Meta-Tool Models
# =============================================================================


class AuditInput(BaseModel):
    """Input for the audit meta-tool.

    Provides query_history action for audit log queries.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Action selector (required)
    action: Literal["help", "query_history"] = Field(..., description="Action to perform")

    # --- Query parameters ---
    case_id: str | None = Field(
        default=None,
        alias="caseId",
        description="Filter by case ID",
    )
    actor: str | None = Field(
        default=None,
        description="Filter by actor email",
    )
    action_type: str | None = Field(
        default=None,
        alias="actionType",
        description="Filter by action type",
    )
    created_after: str | None = Field(
        default=None,
        alias="createdAfter",
        description="Filter: created after this ISO 8601 date",
    )
    created_before: str | None = Field(
        default=None,
        alias="createdBefore",
        description="Filter: created before this ISO 8601 date",
    )
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Results per page (max 200)",
    )


# =============================================================================
# Admin Meta-Tool Models
# =============================================================================


class AdminInput(BaseModel):
    """Input for the admin meta-tool.

    Consolidates health_check and get_server_info operations.
    """

    # Action selector (required)
    action: Literal["help", "health_check", "get_server_info"] = Field(
        ..., description="Action to perform"
    )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "AdminInput",
    "AttachmentsInput",
    "AuditInput",
    "CasesInput",
    "HelpResponse",
    "MessagesInput",
    "MetaToolOutput",
    "SchemaInput",
    "SchemaOutput",
    "TimelineInput",
]
