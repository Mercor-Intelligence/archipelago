"""Pydantic models for Workday HCM MCP tools.

Define API specification using Pydantic models for:
1. Input/output validation
2. Type hints and IDE support
3. Documentation
4. Test generation
"""

import json
from datetime import datetime
from typing import Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, field_validator

# ============================================================================
# CONSTANTS - Employment and Position Status Values
# ============================================================================

VALID_EMPLOYMENT_STATUSES = ["Active", "Terminated", "Leave"]
VALID_POSITION_STATUSES = ["open", "filled", "closed"]
VALID_EVENT_TYPES = ["hire", "termination", "transfer"]
VALID_ORG_TYPES = ["Supervisory", "Cost_Center", "Location"]

# ============================================================================
# BASE OUTPUT MODELS
# ============================================================================


class WorkerOutput(BaseModel):
    """Worker record output model."""

    worker_id: str = Field(
        ...,
        description="Unique worker identifier. Format: alphanumeric string, typically WRK-XXXXX pattern (e.g., 'WRK-00123').",
    )
    job_profile_id: str = Field(
        ...,
        description="Job profile ID. Format: alphanumeric string (e.g., 'JP-ENG-002').",
    )
    org_id: str = Field(
        ...,
        description="Supervisory organization ID. Format: alphanumeric string (e.g., 'ORG-001').",
    )
    cost_center_id: str = Field(
        ..., description="Cost center ID. Format: alphanumeric string (e.g., 'CC-001')."
    )
    location_id: str | None = Field(
        None,
        description="Location ID if assigned. Format: alphanumeric string (e.g., 'LOC-NYC-001').",
    )
    position_id: str | None = Field(
        None,
        description="Position ID if worker is assigned to a position. Format: alphanumeric string, typically POS-XXXXX pattern (e.g., 'POS-00456').",
    )
    employment_status: str = Field(
        ..., description="Employment status. Values: 'Active', 'Terminated', 'Leave'."
    )
    fte: float = Field(
        ...,
        description="Full-time equivalent. Range: 0.0-1.0 where 1.0=full-time, 0.5=half-time.",
    )
    hire_date: str = Field(..., description="Hire date in YYYY-MM-DD format (e.g., '2024-01-15').")
    termination_date: str | None = Field(
        None,
        description="Termination date in YYYY-MM-DD format (e.g., '2024-12-31'). Null if not terminated.",
    )
    effective_date: str = Field(
        ...,
        description="Date the most recent change took effect in YYYY-MM-DD format (e.g., '2024-01-15').",
    )
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )
    updated_at: str = Field(
        ...,
        description="Record last update timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T16:45:30Z').",
    )


class PositionOutput(BaseModel):
    """Position record output model."""

    position_id: str = Field(
        ...,
        description="Unique position identifier. Format: alphanumeric string, typically POS-XXXXX pattern (e.g., 'POS-00456').",
    )
    job_profile_id: str = Field(
        ...,
        description="Job profile ID associated with this position. Format: alphanumeric string (e.g., 'JP-ENG-002').",
    )
    org_id: str = Field(
        ...,
        description="Supervisory organization ID this position belongs to. Format: alphanumeric string (e.g., 'ORG-001').",
    )
    fte: float = Field(
        ...,
        description="FTE allocation for this position. Range: 0.0-1.0 where 1.0=full-time, 0.5=half-time.",
    )
    status: str = Field(
        ...,
        description="Position status. Values: 'open' (available for hire), 'filled' (worker assigned), 'closed' (no longer available).",
    )
    worker_id: str | None = Field(
        None,
        description="Worker ID if position is filled. Null if position is open or closed. Format: WRK-XXXXX (e.g., 'WRK-00123').",
    )
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )
    updated_at: str = Field(
        ...,
        description="Record last update timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T16:45:30Z').",
    )


class SupervisoryOrgOutput(BaseModel):
    """Supervisory organization output model."""

    org_id: str = Field(
        ..., description="Organization ID. Format: alphanumeric string (e.g., 'ORG-001')."
    )
    org_name: str = Field(
        ..., description="Organization display name (e.g., 'Engineering Department')."
    )
    org_type: str = Field(
        ..., description="Organization type. Values: 'Supervisory', 'Cost_Center', 'Location'."
    )
    parent_org_id: str | None = Field(
        None,
        description="Parent organization ID. Null if this is a root organization. Format: alphanumeric string (e.g., 'ORG-ROOT').",
    )
    manager_worker_id: str | None = Field(
        None,
        description="Worker ID of the organization manager. Format: WRK-XXXXX (e.g., 'WRK-00123').",
    )
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )
    updated_at: str = Field(
        ...,
        description="Record last update timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T16:45:30Z').",
    )


class JobProfileOutput(BaseModel):
    """Job profile output model."""

    job_profile_id: str = Field(
        ..., description="Job profile ID. Format: alphanumeric string (e.g., 'JP-ENG-002')."
    )
    title: str = Field(
        ...,
        description="Job title displayed in HR systems (e.g., 'Senior Software Engineer', 'Product Manager'). Max 255 characters.",
    )
    job_family: str = Field(
        ..., description="Job family grouping (e.g., 'Engineering', 'Sales', 'Marketing')."
    )
    job_level: str | None = Field(
        None, description="Job level within the family (e.g., 'L3', 'Senior', 'Director')."
    )
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )


class ListJobProfilesInput(BaseModel):
    """Input model for listing job profiles with pagination and filters."""

    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of results per page. Range: 1-1000. Default: 100.",
    )
    page_number: int = Field(
        default=1, ge=1, description="Page number to retrieve (1-indexed). Default: 1."
    )
    job_family: str | None = Field(
        None, description="Filter by job family (e.g., 'Engineering', 'Sales'). Case-sensitive."
    )


class JobProfileListOutput(BaseModel):
    """Output model for paginated job profile list."""

    job_profiles: list[JobProfileOutput] = Field(
        ..., description="List of job profiles in this page."
    )
    total_count: int = Field(
        ..., description="Total number of job profiles matching the query across all pages."
    )
    page_size: int = Field(
        ..., description="Number of results returned per page (echoes request value)."
    )
    page_number: int = Field(
        ..., description="Current page number (1-indexed, echoes request value)."
    )


class MovementOutput(BaseModel):
    """Movement event output model."""

    event_id: str = Field(
        ...,
        description="Unique event identifier. Format: alphanumeric string (e.g., 'EVT-20240115-001').",
    )
    worker_id: str = Field(
        ..., description="Worker ID involved in this event. Format: WRK-XXXXX (e.g., 'WRK-00123')."
    )
    event_type: str = Field(
        ..., description="Event type. Values: 'hire', 'termination', 'transfer'."
    )
    event_date: str = Field(
        ..., description="Date the event occurred in YYYY-MM-DD format (e.g., '2024-01-15')."
    )
    from_org_id: str | None = Field(
        None, description="Source organization ID for transfers. Null for hire events."
    )
    to_org_id: str | None = Field(
        None, description="Destination organization ID for hires/transfers. Null for terminations."
    )
    from_cost_center_id: str | None = Field(
        None, description="Source cost center ID for transfers. Null for hire events."
    )
    to_cost_center_id: str | None = Field(
        None, description="Destination cost center ID for hires/transfers. Null for terminations."
    )
    from_job_profile_id: str | None = Field(
        None, description="Source job profile ID for transfers. Null for hire events."
    )
    to_job_profile_id: str | None = Field(
        None, description="Destination job profile ID for hires/transfers. Null for terminations."
    )
    from_position_id: str | None = Field(
        None, description="Source position ID for transfers. Null for hire events."
    )
    to_position_id: str | None = Field(
        None, description="Destination position ID for hires/transfers. Null for terminations."
    )
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )


# ============================================================================
# WORKER MANAGEMENT INPUTS
# ============================================================================


class CreateWorkerInput(BaseModel):
    """Input model for creating a new worker (hire action)."""

    worker_id: str = Field(
        ...,
        description="Unique worker identifier. Format: alphanumeric string, typically WRK-XXXXX pattern (e.g., 'WRK-00123'). User-provided, must be unique, immutable after creation.",
    )
    job_profile_id: str = Field(
        ...,
        description="Job profile ID",
        json_schema_extra={
            "x-populate-from": "export_csv",
            "x-populate-field": "rows",
            "x-populate-value": "job_profile_id",
            "x-populate-display": "title",
            "x-populate-dependencies": {
                "table_name": {"const": "job_profiles"},
                "format": {"const": "json"},
                "limit": {"const": 1000},
            },
        },
    )
    org_id: str = Field(
        ...,
        description="Supervisory organization ID",
        json_schema_extra={
            "x-populate-from": "export_csv",
            "x-populate-field": "rows",
            "x-populate-value": "org_id",
            "x-populate-display": "org_name",
            "x-populate-dependencies": {
                "table_name": {"const": "supervisory_orgs"},
                "format": {"const": "json"},
                "limit": {"const": 1000},
            },
        },
    )
    cost_center_id: str = Field(
        ...,
        description="Cost center ID",
        json_schema_extra={
            "x-populate-from": "export_csv",
            "x-populate-field": "rows",
            "x-populate-value": "cost_center_id",
            "x-populate-display": "cost_center_name",
            "x-populate-dependencies": {
                "table_name": {"const": "cost_centers"},
                "format": {"const": "json"},
                "limit": {"const": 1000},
                "filters": {"org_id": "org_id"},
            },
        },
    )
    location_id: str | None = Field(
        None,
        description="Location ID",
        json_schema_extra={
            "x-populate-from": "export_csv",
            "x-populate-field": "rows",
            "x-populate-value": "location_id",
            "x-populate-display": "location_name",
            "x-populate-dependencies": {
                "table_name": {"const": "locations"},
                "format": {"const": "json"},
                "limit": {"const": 1000},
            },
        },
    )
    position_id: str | None = Field(
        None,
        description="Position ID to fill",
        json_schema_extra={
            "x-populate-from": "export_csv",
            "x-populate-field": "rows",
            "x-populate-value": "position_id",
            "x-populate-display": "position_id",
            "x-populate-dependencies": {
                "table_name": {"const": "positions"},
                "format": {"const": "json"},
                "limit": {"const": 1000},
                "filters": {
                    "org_id": "org_id",
                    "job_profile_id": "job_profile_id",
                    "status": {"const": "open"},
                },
            },
        },
    )
    fte: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Full-time equivalent. Range: 0.0-1.0 where 1.0=full-time, 0.5=half-time. Default: 1.0.",
    )
    hire_date: str = Field(
        ...,
        description="Employment start date in YYYY-MM-DD format (e.g., '2024-03-15'). Must be today or a future date.",
    )
    effective_date: str | None = Field(
        None,
        description="Date the hire takes effect in Workday systems in YYYY-MM-DD format (e.g., '2024-03-15'). Defaults to hire_date if not provided.",
    )

    @field_validator("fte")
    @classmethod
    def validate_fte(cls, v: float) -> float:
        """Validate FTE is between 0.0 and 1.0."""
        if not 0.0 <= v <= 1.0:
            raise ValueError("FTE must be between 0.0 and 1.0")
        return v


class TerminateWorkerInput(BaseModel):
    """Input model for ending active employment - either permanent termination or temporary leave.

    Use this tool when:
    - Terminating employment permanently (new_status='Terminated')
    - Placing worker on leave of absence (new_status='Leave')

    Do NOT use this tool to:
    - Return a worker from leave (use transfer with employment_status change)
    - Update other worker attributes (use transfer)
    """

    worker_id: str = Field(
        ...,
        description="Worker ID to terminate or place on leave. Worker must currently have 'Active' status. Format: WRK-XXXXX (e.g., 'WRK-00123').",
    )
    new_status: Literal["Terminated", "Leave"] = Field(
        default="Terminated",
        description="Target employment status. 'Terminated' = permanent end of employment (worker's position is released). 'Leave' = temporary absence (e.g., parental leave, sabbatical - position is retained). Default: 'Terminated'.",
    )
    status_date: str = Field(
        ...,
        description="Date the status change occurred in real life in YYYY-MM-DD format (e.g., '2024-03-15'). This is the employee's last working day for termination, or leave start date.",
    )
    effective_date: str | None = Field(
        None,
        description="Date the change takes effect in Workday systems in YYYY-MM-DD format (e.g., '2024-03-15'). Defaults to status_date if not provided. Use for backdating or future-dating changes.",
    )

    # Deprecated fields for backward compatibility
    termination_date: str | None = Field(
        None, description="DEPRECATED: Use status_date instead. Termination date (YYYY-MM-DD)"
    )

    def __init__(self, **data):
        """Initialize with backward compatibility for termination_date."""
        # Handle backward compatibility: if termination_date provided but not status_date
        if "termination_date" in data and "status_date" not in data:
            data["status_date"] = data["termination_date"]
            data["new_status"] = "Terminated"
        super().__init__(**data)


class TransferWorkerInput(BaseModel):
    """Input model for transferring a worker to a new org, position, or role.

    At least one of the optional fields (new_org_id, new_cost_center_id, new_job_profile_id,
    new_position_id, new_fte) must be provided.
    """

    worker_id: str = Field(
        ...,
        description="Worker ID to transfer. Worker must have 'Active' status. Format: WRK-XXXXX (e.g., 'WRK-00123').",
    )
    new_org_id: str | None = Field(
        None,
        description="New supervisory organization ID. Format: alphanumeric string (e.g., 'ORG-002'). Worker's cost center may need to be updated to one associated with the new org.",
    )
    new_cost_center_id: str | None = Field(
        None,
        description="New cost center ID. Format: alphanumeric string (e.g., 'CC-002'). Must be associated with the worker's organization.",
    )
    new_job_profile_id: str | None = Field(
        None,
        description="New job profile ID for promotion/demotion/lateral move. Format: alphanumeric string (e.g., 'JP-ENG-003').",
    )
    new_position_id: str | None = Field(
        None,
        description="New position ID to assign. The worker's current position (if any) will be automatically released and set to 'open' status. New position must exist and have 'open' status. Format: POS-XXXXX (e.g., 'POS-00789').",
    )
    new_fte: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="New FTE value. Range: 0.0-1.0 where 1.0=full-time, 0.5=half-time.",
    )
    transfer_date: str = Field(
        ...,
        description="Transfer effective date in YYYY-MM-DD format (e.g., '2024-03-15').",
    )
    effective_date: str | None = Field(
        None,
        description="Date the change takes effect in Workday systems in YYYY-MM-DD format. Defaults to transfer_date if not provided.",
    )

    @field_validator("new_fte")
    @classmethod
    def validate_fte(cls, v: float | None) -> float | None:
        """Validate FTE is between 0.0 and 1.0."""
        if v is not None and not 0.0 <= v <= 1.0:
            raise ValueError("FTE must be between 0.0 and 1.0")
        return v


class GetWorkerInput(BaseModel):
    """Input model for retrieving a worker."""

    worker_id: str = Field(
        ..., description="Worker ID to retrieve. Format: WRK-XXXXX (e.g., 'WRK-00123')."
    )
    as_of_date: str | None = Field(
        None,
        description="Point-in-time query date in YYYY-MM-DD format (e.g., '2024-01-15'). Returns worker state as of that date. If null, returns current state.",
    )


class ListWorkersInput(BaseModel):
    """Input model for listing workers with pagination and filters."""

    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of results per page. Range: 1-1000. Default: 100.",
    )
    page_number: int = Field(
        default=1, ge=1, description="Page number to retrieve (1-indexed). Default: 1."
    )
    org_id: str | None = Field(
        None,
        description="Filter by organization ID. Format: alphanumeric string (e.g., 'ORG-001').",
    )
    cost_center_id: str | None = Field(
        None,
        description="Filter by cost center ID. Format: alphanumeric string (e.g., 'CC-001').",
    )
    employment_status: Literal["Active", "Terminated", "Leave"] | None = Field(
        None,
        description="Filter by employment status. Values: 'Active' (currently employed), 'Terminated' (employment ended), 'Leave' (on leave of absence).",
    )
    as_of_date: str | None = Field(
        None,
        description="Point-in-time query date in YYYY-MM-DD format (e.g., '2024-01-15'). Returns worker states as of that date. If null, returns current states.",
    )


class WorkerListOutput(BaseModel):
    """Output model for paginated worker list."""

    workers: list[WorkerOutput] = Field(..., description="List of workers in this page.")
    total_count: int = Field(
        ..., description="Total number of workers matching the query across all pages."
    )
    page_size: int = Field(
        ..., description="Number of results returned per page (echoes request value)."
    )
    page_number: int = Field(
        ..., description="Current page number (1-indexed, echoes request value)."
    )


# ============================================================================
# POSITION MANAGEMENT INPUTS
# ============================================================================


class CreatePositionInput(BaseModel):
    """Input model for creating a new position."""

    position_id: str = Field(
        ...,
        description="Unique position identifier. Format: alphanumeric string, typically POS-XXXXX pattern (e.g., 'POS-00456'). User-provided, must be unique.",
    )
    job_profile_id: str = Field(
        ...,
        description="Job profile ID",
        json_schema_extra={
            "x-populate-from": "export_csv",
            "x-populate-field": "rows",
            "x-populate-value": "job_profile_id",
            "x-populate-display": "title",
            "x-populate-dependencies": {
                "table_name": {"const": "job_profiles"},
                "format": {"const": "json"},
                "limit": {"const": 1000},
            },
        },
    )
    org_id: str = Field(
        ...,
        description="Supervisory organization ID",
        json_schema_extra={
            "x-populate-from": "export_csv",
            "x-populate-field": "rows",
            "x-populate-value": "org_id",
            "x-populate-display": "org_name",
            "x-populate-dependencies": {
                "table_name": {"const": "supervisory_orgs"},
                "format": {"const": "json"},
                "limit": {"const": 1000},
            },
        },
    )
    fte: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="FTE allocation for this position. Range: 0.0-1.0 where 1.0=full-time, 0.5=half-time. Default: 1.0.",
    )
    status: Literal["open"] = Field(
        default="open",
        description="Initial position status. New positions are always created with 'open' status.",
    )

    @field_validator("fte")
    @classmethod
    def validate_fte(cls, v: float) -> float:
        """Validate FTE is between 0.0 and 1.0."""
        if not 0.0 <= v <= 1.0:
            raise ValueError("FTE must be between 0.0 and 1.0")
        return v


class GetPositionInput(BaseModel):
    """Input model for retrieving a position."""

    position_id: str = Field(
        ...,
        description="Position ID to retrieve. Format: POS-XXXXX (e.g., 'POS-00456').",
    )


class ListPositionsInput(BaseModel):
    """Input model for listing positions with pagination and filters."""

    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of results per page. Range: 1-1000. Default: 100.",
    )
    page_number: int = Field(
        default=1, ge=1, description="Page number to retrieve (1-indexed). Default: 1."
    )
    org_id: str | None = Field(
        None,
        description="Filter by organization ID. Format: alphanumeric string (e.g., 'ORG-001').",
    )
    status: Literal["open", "filled", "closed"] | None = Field(
        None,
        description="Filter by position status. Values: 'open' (available), 'filled' (worker assigned), 'closed' (no longer available).",
    )
    job_profile_id: str | None = Field(
        None,
        description="Filter by job profile ID. Format: alphanumeric string (e.g., 'JP-ENG-002').",
    )


class PositionListOutput(BaseModel):
    """Output model for paginated position list."""

    positions: list[PositionOutput] = Field(..., description="List of positions in this page.")
    total_count: int = Field(
        ..., description="Total number of positions matching the query across all pages."
    )
    page_size: int = Field(
        ..., description="Number of results returned per page (echoes request value)."
    )
    page_number: int = Field(
        ..., description="Current page number (1-indexed, echoes request value)."
    )


class ClosePositionInput(BaseModel):
    """Input model for closing a position."""

    position_id: str = Field(
        ...,
        description="Position ID to close. Position must have 'open' status (unfilled). Format: POS-XXXXX (e.g., 'POS-00456').",
    )


# ============================================================================
# ORGANIZATIONAL HIERARCHY INPUTS
# ============================================================================


class CreateSupervisoryOrgInput(BaseModel):
    """Input model for creating a supervisory organization."""

    org_id: str = Field(
        ...,
        description="Organization ID. Format: alphanumeric string (e.g., 'ORG-001'). User-provided, must be unique.",
    )
    org_name: str = Field(
        ...,
        description="Organization display name (e.g., 'Engineering Department'). Max 255 characters.",
    )
    org_type: Literal["Supervisory", "Cost_Center", "Location"] = Field(
        default="Supervisory",
        description="Organization type. Values: 'Supervisory' (management hierarchy), 'Cost_Center' (budget allocation), 'Location' (physical location). Default: 'Supervisory'.",
    )
    parent_org_id: str | None = Field(
        None,
        description="Parent organization ID. Null for root organizations. Format: alphanumeric string (e.g., 'ORG-ROOT').",
    )
    manager_worker_id: str | None = Field(
        None,
        description="Worker ID of the organization manager. Format: WRK-XXXXX (e.g., 'WRK-00123').",
    )

    @field_validator("parent_org_id", "manager_worker_id", mode="before")
    @classmethod
    def empty_string_to_none(cls, v: str | None) -> str | None:
        """Convert empty string to None to avoid FK constraint violations."""
        return None if v == "" else v


class GetSupervisoryOrgInput(BaseModel):
    """Input model for retrieving a supervisory organization."""

    org_id: str = Field(
        ...,
        description="Organization ID to retrieve. Format: alphanumeric string (e.g., 'ORG-001').",
    )


class ListSupervisoryOrgsInput(BaseModel):
    """Input model for listing supervisory organizations."""

    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of results per page. Range: 1-1000. Default: 100.",
    )
    page_number: int = Field(
        default=1, ge=1, description="Page number to retrieve (1-indexed). Default: 1."
    )
    parent_org_id: str | None = Field(
        None,
        description="Filter by parent organization ID. Format: alphanumeric string (e.g., 'ORG-ROOT').",
    )
    org_type: Literal["Supervisory", "Cost_Center", "Location"] | None = Field(
        None,
        description="Filter by organization type. Values: 'Supervisory', 'Cost_Center', 'Location'.",
    )
    root_only: bool = Field(
        default=False,
        description="If true, returns only root organizations (those with no parent). Default: false.",
    )

    @field_validator("parent_org_id", mode="before")
    @classmethod
    def empty_string_to_none(cls, v: str | None) -> str | None:
        """Convert empty string to None to avoid confusing API behavior."""
        return None if v == "" else v


class SupervisoryOrgListOutput(BaseModel):
    """Output model for paginated supervisory organization list."""

    orgs: list[SupervisoryOrgOutput] = Field(..., description="List of organizations in this page.")
    total_count: int = Field(
        ..., description="Total number of organizations matching the query across all pages."
    )
    page_size: int = Field(
        ..., description="Number of results returned per page (echoes request value)."
    )
    page_number: int = Field(
        ..., description="Current page number (1-indexed, echoes request value)."
    )


class GetOrgHierarchyInput(BaseModel):
    """Input model for retrieving organization hierarchy."""

    root_org_id: str | None = Field(
        None,
        description="Root organization ID to start the hierarchy from. Format: alphanumeric string (e.g., 'ORG-ROOT'). If null, returns the full organizational tree.",
    )


class OrgHierarchyNode(BaseModel):
    """Nested organization hierarchy node with recursive children."""

    org_id: str = Field(..., description="Organization ID")
    org_name: str = Field(..., description="Organization name")
    org_type: str = Field(..., description="Organization type")
    manager_worker_id: str | None = Field(None, description="Manager worker ID")
    children: list["OrgHierarchyNode"] = Field(
        default_factory=list, description="Child organizations (recursive)"
    )


class OrgHierarchyOutput(BaseModel):
    """Output model for organization hierarchy tree structure."""

    hierarchy: list[OrgHierarchyNode] = Field(
        ..., description="List of root nodes in the hierarchy"
    )


# ============================================================================
# MOVEMENT HISTORY INPUTS
# ============================================================================


class ListMovementsInput(BaseModel):
    """Input model for listing movement events."""

    page_size: int = Field(default=100, ge=1, le=1000, description="Results per page")
    page_number: int = Field(default=1, ge=1, description="Page number")
    worker_id: str | None = Field(None, description="Filter by worker ID")
    event_type: Literal["hire", "termination", "transfer"] | None = Field(
        None, description="Filter by event type. Options: hire, termination, transfer"
    )
    start_date: str | None = Field(
        None, description="Range start (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    end_date: str | None = Field(
        None, description="Range end (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )


class MovementListOutput(BaseModel):
    """Output model for paginated movement list."""

    movements: list[MovementOutput] = Field(..., description="List of movement events")
    total_count: int = Field(..., description="Total number of matching movements")
    page_size: int = Field(..., description="Results per page. Range: 1-100. Default: 25.")
    page_number: int = Field(..., description="Target page (1-indexed). REQUIRED.")


# ============================================================================
# JOB PROFILE MANAGEMENT INPUTS
# ============================================================================


class GetJobProfileInput(BaseModel):
    """Input model for retrieving a job profile."""

    job_profile_id: str = Field(
        ...,
        description="Job profile ID to retrieve. Format: alphanumeric string (e.g., 'JP-ENG-002').",
    )


class CreateJobProfileInput(BaseModel):
    """Input model for creating a job profile."""

    job_profile_id: str = Field(
        ...,
        description="Unique job profile identifier. Format: alphanumeric string (e.g., 'JP-ENG-002'). User-provided, must be unique.",
    )
    title: str = Field(
        ...,
        description="Job title displayed in HR systems (e.g., 'Senior Software Engineer', 'Product Manager'). Max 255 characters.",
    )
    job_family: str = Field(
        ..., description="Job family grouping (e.g., 'Engineering', 'Sales', 'Marketing')."
    )
    job_level: str | None = Field(
        None, description="Job level within the family (e.g., 'L3', 'Senior', 'Director')."
    )


# ============================================================================
# RAAS REPORTING INPUTS
# ============================================================================


class WorkforceRosterInput(BaseModel):
    """Input model for workforce roster report."""

    org_id: str | None = Field(None, description="Filter by organization")
    cost_center_id: str | None = Field(None, description="Filter by cost center")
    employment_status: Literal["Active", "Terminated", "Leave"] | None = Field(
        None, description="Filter by employment status. Options: Active, Terminated, Leave"
    )
    as_of_date: str | None = Field(None, description="Point-in-time roster (YYYY-MM-DD)")
    page_size: int = Field(default=1000, ge=1, le=10000, description="Results per page")
    page_number: int = Field(default=1, ge=1, description="Page number")

    @field_validator("as_of_date")
    @classmethod
    def validate_as_of_date_format(cls, v: str | None) -> str | None:
        """Validate as_of_date is in strict YYYY-MM-DD format."""
        if v is not None:
            if len(v) != 10:
                raise ValueError("as_of_date must be in YYYY-MM-DD format")
            if v[4] != "-" or v[7] != "-":
                raise ValueError("as_of_date must be in YYYY-MM-DD format")
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise ValueError("as_of_date must be in YYYY-MM-DD format")
        return v


class WorkforceRosterRow(BaseModel):
    """Single row in workforce roster report."""

    worker_id: str = Field(..., description="Worker ID")
    job_profile_id: str = Field(..., description="Job profile ID")
    job_title: str = Field(..., description="Job title (joined from job_profiles)")
    job_family: str = Field(..., description="Job family")
    org_id: str = Field(..., description="Organization ID")
    org_name: str = Field(..., description="Organization name (joined from supervisory_orgs)")
    cost_center_id: str = Field(..., description="Cost center ID")
    cost_center_name: str = Field(..., description="Cost center name (joined from cost_centers)")
    location_id: str | None = Field(None, description="Location ID")
    location_name: str | None = Field(None, description="Location name (joined from locations)")
    employment_status: str = Field(..., description="Employment status")
    fte: float = Field(..., description="Full-time equivalent")
    hire_date: str = Field(..., description="Hire date (YYYY-MM-DD)")
    termination_date: str | None = Field(None, description="Termination date (YYYY-MM-DD)")
    effective_date: str = Field(..., description="Date change takes effect (YYYY-MM-DD). REQUIRED.")


class WorkforceRosterOutput(BaseModel):
    """Output model for workforce roster report."""

    roster: list[WorkforceRosterRow] = Field(..., description="List of workers in roster")
    total_count: int = Field(..., description="Total number of matching workers")
    page_size: int = Field(..., description="Results per page. Range: 1-100. Default: 25.")
    page_number: int = Field(..., description="Target page (1-indexed). REQUIRED.")
    as_of_date: str | None = Field(None, description="Point-in-time date if specified")


class HeadcountReportInput(BaseModel):
    """Input model for headcount reconciliation report."""

    start_date: str = Field(
        ..., description="Range start (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    end_date: str = Field(
        ..., description="Range end (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    group_by: Literal["org_id", "cost_center_id"] = Field(
        default="org_id", description="Grouping dimension. Options: org_id, cost_center_id"
    )
    org_id: str | None = Field(None, description="Filter by organization")


class HeadcountReportRow(BaseModel):
    """Single row in headcount report."""

    group_id: str = Field(..., description="Organization or cost center ID")
    group_name: str = Field(..., description="Organization or cost center name")
    beginning_hc: int = Field(..., description="Active workers at start_date")
    hires: int = Field(..., description="Workers hired during period")
    terminations: int = Field(..., description="Workers terminated during period")
    transfers_in: int = Field(..., description="Transfers into this group")
    transfers_out: int = Field(..., description="Transfers out of this group")
    net_movement: int = Field(
        ..., description="hires - terminations + transfers_in - transfers_out"
    )
    ending_hc: int = Field(..., description="beginning_hc + net_movement")


class HeadcountReportOutput(BaseModel):
    """Output model for headcount reconciliation report."""

    report: list[HeadcountReportRow] = Field(..., description="Headcount report rows")
    total_count: int = Field(..., description="Total number of groups")
    start_date: str = Field(
        ..., description="Range start (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    end_date: str = Field(
        ..., description="Range end (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    group_by: str = Field(..., description="Grouping dimension")


# ============================================================================
# MOVEMENT REPORT INPUTS
# ============================================================================


class MovementReportInput(BaseModel):
    """Input model for movement report."""

    start_date: str = Field(
        ..., description="Range start (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    end_date: str = Field(
        ..., description="Range end (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    event_type: Literal["hire", "termination", "transfer"] | None = Field(
        None, description="Filter by event type. Options: hire, termination, transfer"
    )
    org_id: str | None = Field(
        None, description="Filter by organization ID (to_org_id or from_org_id)"
    )
    page_size: int = Field(default=1000, ge=1, le=10000, description="Results per page")
    page_number: int = Field(default=1, ge=1, description="Page number")


class MovementReportRow(BaseModel):
    """Single row in movement report."""

    event_id: str = Field(..., description="Event ID")
    worker_id: str = Field(..., description="Worker ID")
    event_type: str = Field(..., description="Event type (hire|termination|transfer)")
    event_date: str = Field(..., description="Event date (YYYY-MM-DD)")
    from_org_id: str | None = Field(None, description="Source organization ID")
    from_org_name: str | None = Field(None, description="Source organization name")
    to_org_id: str | None = Field(None, description="Destination organization ID")
    to_org_name: str | None = Field(None, description="Destination organization name")
    from_cost_center_id: str | None = Field(None, description="Source cost center ID")
    from_cost_center_name: str | None = Field(None, description="Source cost center name")
    to_cost_center_id: str | None = Field(None, description="Destination cost center ID")
    to_cost_center_name: str | None = Field(None, description="Destination cost center name")
    from_job_profile_id: str | None = Field(None, description="Source job profile ID")
    from_job_title: str | None = Field(None, description="Source job title")
    to_job_profile_id: str | None = Field(None, description="Destination job profile ID")
    to_job_title: str | None = Field(None, description="Destination job title")
    from_position_id: str | None = Field(None, description="Source position ID")
    to_position_id: str | None = Field(None, description="Destination position ID")
    created_at: str = Field(..., description="Record creation timestamp")


class MovementReportOutput(BaseModel):
    """Output model for movement report."""

    movements: list[MovementReportRow] = Field(..., description="List of movement events")
    total_count: int = Field(..., description="Total number of matching movements")
    page_size: int = Field(..., description="Results per page. Range: 1-100. Default: 25.")
    page_number: int = Field(..., description="Target page (1-indexed). REQUIRED.")
    start_date: str = Field(
        ..., description="Range start (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    end_date: str = Field(
        ..., description="Range end (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )


# ============================================================================
# POSITION REPORT INPUTS
# ============================================================================


class PositionReportInput(BaseModel):
    """Input model for position vacancy report."""

    org_id: str | None = Field(None, description="Filter by organization")
    status: Literal["open", "filled", "closed"] | None = Field(
        None, description="Filter by position status. Options: open, filled, closed"
    )
    job_profile_id: str | None = Field(None, description="Filter by job profile")
    page_size: int = Field(default=1000, ge=1, le=10000, description="Results per page")
    page_number: int = Field(default=1, ge=1, description="Page number")


class PositionReportRow(BaseModel):
    """Single row in position report."""

    position_id: str = Field(..., description="Position ID")
    job_profile_id: str = Field(..., description="Job profile ID")
    job_title: str = Field(..., description="Job title (joined from job_profiles)")
    job_family: str = Field(..., description="Job family")
    org_id: str = Field(..., description="Organization ID")
    org_name: str = Field(..., description="Organization name (joined from supervisory_orgs)")
    fte: float = Field(..., description="FTE allocation")
    status: str = Field(
        ...,
        description="Position status. Values: 'open' (available for hire), 'filled' (worker assigned), 'closed' (no longer available).",
    )
    worker_id: str | None = Field(
        None, description="Worker ID if position is filled. Format: WRK-XXXXX (e.g., 'WRK-00123')."
    )
    days_open: int | None = Field(
        None, description="Number of days position has been open. Null if status is not 'open'."
    )
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )


class PositionReportOutput(BaseModel):
    """Output model for position report."""

    positions: list[PositionReportRow] = Field(..., description="List of positions")
    total_count: int = Field(..., description="Total number of matching positions")
    open_positions: int = Field(..., description="Count of open positions")
    filled_positions: int = Field(..., description="Count of filled positions")
    closed_positions: int = Field(..., description="Count of closed positions")
    page_size: int = Field(..., description="Results per page. Range: 1-100. Default: 25.")
    page_number: int = Field(..., description="Target page (1-indexed). REQUIRED.")


# ============================================================================
# ORG HIERARCHY REPORT INPUTS
# ============================================================================


class OrgHierarchyReportInput(BaseModel):
    """Input model for organization hierarchy report."""

    root_org_id: str | None = Field(None, description="Root org ID to start from")


class OrgHierarchyReportRow(BaseModel):
    """Single row in organization hierarchy report."""

    org_id: str = Field(..., description="Organization ID")
    org_name: str = Field(..., description="Organization name")
    org_type: str = Field(..., description="Organization type")
    parent_org_id: str | None = Field(None, description="Parent organization ID")
    parent_org_name: str | None = Field(None, description="Parent organization name")
    org_level: int = Field(..., description="Depth in hierarchy (0 = root)")
    manager_worker_id: str | None = Field(None, description="Manager worker ID")
    headcount: int = Field(..., description="Current active workers in this org")


class OrgHierarchyReportOutput(BaseModel):
    """Output model for organization hierarchy report."""

    hierarchy: list[OrgHierarchyReportRow] = Field(
        ..., description="List of organizations in hierarchy"
    )
    total_count: int = Field(..., description="Total number of organizations")


# =============================================================================
# V2 PRE-ONBOARDING COORDINATION MODELS
# =============================================================================

# V2 Constants
VALID_CASE_STATUSES = ["open", "in_progress", "pending_approval", "resolved", "closed"]
VALID_CASE_EMPLOYMENT_TYPES = ["full_time", "part_time", "contractor"]
VALID_MILESTONE_TYPES = ["screening", "work_authorization", "documents", "approvals"]
VALID_MILESTONE_STATUSES = ["pending", "in_progress", "completed", "waived", "blocked"]
VALID_TASK_STATUSES = ["pending", "in_progress", "completed", "cancelled"]
VALID_POLICY_TYPES = ["prerequisites", "lead_times", "payroll_cutoffs", "constraints"]
VALID_APPROVAL_STATUSES = ["pending", "approved", "denied"]
VALID_ONBOARDING_STATUSES = ["not_started", "in_progress", "ready", "finalized"]
VALID_HCM_WRITE_TYPES = ["confirm_start_date", "update_readiness", "finalize_hire"]
VALID_PERSONAS = [
    "pre_onboarding_coordinator",
    "hr_admin",
    "hr_business_partner",
    "hiring_manager",
    "auditor",
]

VALID_COUNTRY_CODES = [
    "AF",
    "AL",
    "DZ",
    "AS",
    "AD",
    "AO",
    "AI",
    "AQ",
    "AG",
    "AR",
    "AM",
    "AW",
    "AU",
    "AT",
    "AZ",
    "BS",
    "BH",
    "BD",
    "BB",
    "BY",
    "BE",
    "BZ",
    "BJ",
    "BM",
    "BT",
    "BO",
    "BA",
    "BW",
    "BV",
    "BR",
    "IO",
    "BN",
    "BG",
    "BF",
    "BI",
    "CV",
    "KH",
    "CM",
    "CA",
    "KY",
    "CF",
    "TD",
    "CL",
    "CN",
    "CX",
    "CC",
    "CO",
    "KM",
    "CD",
    "CG",
    "CK",
    "CR",
    "HR",
    "CU",
    "CW",
    "CY",
    "CZ",
    "DK",
    "DJ",
    "DM",
    "DO",
    "EC",
    "EG",
    "SV",
    "GQ",
    "ER",
    "EE",
    "SZ",
    "ET",
    "FK",
    "FO",
    "FJ",
    "FI",
    "FR",
    "GF",
    "PF",
    "GA",
    "GM",
    "GE",
    "DE",
    "GH",
    "GI",
    "GR",
    "GL",
    "GD",
    "GP",
    "GU",
    "GT",
    "GG",
    "GN",
    "GW",
    "GY",
    "HT",
    "HM",
    "VA",
    "HN",
    "HK",
    "HU",
    "IS",
    "IN",
    "ID",
    "IR",
    "IQ",
    "IE",
    "IM",
    "IL",
    "IT",
    "JM",
    "JP",
    "JE",
    "JO",
    "KZ",
    "KE",
    "KI",
    "KP",
    "KR",
    "KW",
    "KG",
    "LA",
    "LV",
    "LB",
    "LS",
    "LR",
    "LY",
    "LI",
    "LT",
    "LU",
    "MO",
    "MG",
    "MW",
    "MY",
    "MV",
    "ML",
    "MT",
    "MH",
    "MQ",
    "MR",
    "MU",
    "YT",
    "MX",
    "FM",
    "MD",
    "MC",
    "MN",
    "ME",
    "MS",
    "MA",
    "MZ",
    "MM",
    "NA",
    "NR",
    "NP",
    "NL",
    "NC",
    "NZ",
    "NI",
    "NE",
    "NG",
    "NU",
    "NF",
    "MP",
    "NO",
    "OM",
    "PK",
    "PW",
    "PS",
    "PA",
    "PG",
    "PY",
    "PE",
    "PH",
    "PN",
    "PL",
    "PT",
    "PR",
    "QA",
    "RE",
    "RO",
    "RU",
    "RW",
    "BL",
    "SH",
    "KN",
    "LC",
    "MF",
    "PM",
    "VC",
    "WS",
    "SM",
    "ST",
    "SA",
    "SN",
    "RS",
    "SC",
    "SL",
    "SG",
    "SX",
    "SK",
    "SI",
    "SB",
    "SO",
    "ZA",
    "GS",
    "SS",
    "ES",
    "LK",
    "SD",
    "SR",
    "SJ",
    "SE",
    "CH",
    "SY",
    "TJ",
    "TZ",
    "TH",
    "TL",
    "TG",
    "TK",
    "TO",
    "TT",
    "TN",
    "TR",
    "TM",
    "TC",
    "TV",
    "TW",
    "UG",
    "UA",
    "AE",
    "GB",
    "US",
    "UY",
    "UZ",
    "VU",
    "VE",
    "VN",
    "WF",
    "EH",
    "YE",
    "ZM",
    "ZW",
    "AX",
    "BQ",
]

# =============================================================================
# V2 CASE MANAGEMENT MODELS
# =============================================================================


class MilestoneOutput(BaseModel):
    """Milestone record output model."""

    milestone_id: str = Field(
        ...,
        description="Unique milestone identifier. Format: alphanumeric string (e.g., 'MS-CASE001-screening').",
    )
    case_id: str = Field(..., description="Parent case ID. Format: CASE-XXX (e.g., 'CASE-001').")
    milestone_type: str = Field(
        ...,
        description="Milestone type. Values: 'screening' (background check), 'work_authorization' (visa/work permit), 'documents' (required paperwork), 'approvals' (management sign-offs).",
    )
    status: str = Field(
        ...,
        description="Milestone status. Values: 'pending' (not started), 'in_progress' (being worked on), 'completed' (finished successfully), 'waived' (requirement bypassed via exception), 'blocked' (cannot proceed).",
    )
    evidence_link: str | None = Field(
        None,
        description="URL or reference to evidence supporting milestone completion (e.g., 'https://docs.company.com/bgcheck/123').",
    )
    completion_date: str | None = Field(
        None,
        description="Completion date in YYYY-MM-DD format (e.g., '2024-02-15'). Null if not completed.",
    )
    completed_by: str | None = Field(
        None,
        description="Persona who completed the milestone. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )
    notes: str | None = Field(None, description="Additional notes for audit trail purposes.")
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )
    updated_at: str = Field(
        ...,
        description="Record last update timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T16:45:30Z').",
    )


class TaskOutput(BaseModel):
    """Task record output model."""

    task_id: str = Field(
        ...,
        description="Unique task identifier. Format: alphanumeric string (e.g., 'TASK-001').",
    )
    case_id: str = Field(..., description="Parent case ID. Format: CASE-XXX (e.g., 'CASE-001').")
    milestone_id: str | None = Field(
        None,
        description="Linked milestone ID if this task is associated with a milestone. Null if standalone task.",
    )
    title: str = Field(
        ...,
        description="Brief task description (e.g., 'Complete I-9 verification', 'Schedule orientation'). Max 255 characters.",
    )
    owner_persona: str = Field(
        ...,
        description="Persona responsible for this task. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )
    due_date: str | None = Field(
        None,
        description="Task due date in YYYY-MM-DD format (e.g., '2024-02-28'). Null if no deadline.",
    )
    status: str = Field(
        ...,
        description="Task status. Values: 'pending' (not started), 'in_progress' (being worked on), 'completed' (finished), 'cancelled' (no longer needed).",
    )
    notes: str | None = Field(None, description="Additional notes for audit trail purposes.")
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )
    updated_at: str = Field(
        ...,
        description="Record last update timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T16:45:30Z').",
    )


class CaseOutput(BaseModel):
    """Case record output model."""

    case_id: str = Field(
        ...,
        description="Unique case identifier. Format: CASE-XXX (e.g., 'CASE-001').",
    )
    candidate_id: str = Field(
        ...,
        description="Candidate identifier from ATS. Format: alphanumeric string (e.g., 'CAND-12345').",
    )
    requisition_id: str | None = Field(
        None,
        description="Requisition ID linking to the job posting. Format: alphanumeric string (e.g., 'REQ-2024-001').",
    )
    role: str = Field(
        ...,
        description="Job role/title the candidate is being hired for (e.g., 'Software Engineer').",
    )
    country: str = Field(
        ...,
        description="ISO 3166-1 alpha-2 country code where the worker will be based (e.g., 'US', 'GB', 'DE').",
    )
    employment_type: str = Field(
        ...,
        description="Employment type. Values: 'full_time', 'part_time', 'contractor'.",
    )
    owner_persona: str = Field(
        ...,
        description="Persona responsible for this case. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )
    status: str = Field(
        ...,
        description="Case status. Values: 'open' (new case), 'in_progress' (being worked on), 'pending_approval' (awaiting sign-off), 'resolved' (work completed), 'closed' (finalized).",
    )
    proposed_start_date: str | None = Field(
        None,
        description="Initial proposed start date in YYYY-MM-DD format (e.g., '2024-03-01'). May differ from confirmed_start_date.",
    )
    confirmed_start_date: str | None = Field(
        None,
        description="Final confirmed start date in YYYY-MM-DD format (e.g., '2024-03-15'). Null until confirmed by HCM write-back.",
    )
    due_date: str | None = Field(
        None,
        description="Case deadline in YYYY-MM-DD format (e.g., '2024-02-15'). Null if no deadline set.",
    )
    milestones: list[MilestoneOutput] = Field(
        default_factory=list, description="List of milestones for this case."
    )
    created_at: str = Field(
        ...,
        description="Record creation timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )
    updated_at: str = Field(
        ...,
        description="Record last update timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T16:45:30Z').",
    )


class CreateCaseInput(BaseModel):
    """Input model for creating a new pre-onboarding case."""

    case_id: str = Field(
        ...,
        description="Unique case identifier. Format: CASE-XXX (e.g., 'CASE-001'). User-provided, must be unique.",
    )
    candidate_id: str = Field(
        ...,
        description="Candidate identifier from ATS. Format: alphanumeric string (e.g., 'CAND-12345').",
    )
    requisition_id: str | None = Field(
        None,
        description="Requisition ID linking to the job posting. Format: alphanumeric string (e.g., 'REQ-2024-001').",
    )
    role: str = Field(
        ...,
        description="Job role/title the candidate is being hired for (e.g., 'Software Engineer').",
    )
    country: str = Field(
        ...,
        description="ISO 3166-1 alpha-2 country code where the worker will be based (e.g., 'US', 'GB', 'DE'). Use uppercase two-letter codes.",
    )
    employment_type: Literal["full_time", "part_time", "contractor"] = Field(
        default="full_time",
        description="Employment type. Values: 'full_time' (standard employee), 'part_time' (reduced hours), 'contractor' (non-employee). Default: 'full_time'.",
    )
    owner_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        default="pre_onboarding_coordinator",
        description="Persona responsible for this case. Values: 'pre_onboarding_coordinator' (default), 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )
    proposed_start_date: str | None = Field(
        None,
        description="Initial proposed start date in YYYY-MM-DD format (e.g., '2024-03-15'). Must be a future date.",
    )
    due_date: str | None = Field(
        None,
        description="Case deadline in YYYY-MM-DD format (e.g., '2024-02-28'). Should be before proposed_start_date.",
    )
    notes: str | None = Field(None, description="Additional notes for audit trail purposes.")


class GetCaseInput(BaseModel):
    """Input model for retrieving a case."""

    case_id: str = Field(..., description="Case ID to retrieve")
    include_tasks: bool = Field(default=True, description="Include associated tasks")
    include_audit: bool = Field(default=False, description="Include audit trail")


class AuditEntryOutput(BaseModel):
    """Audit entry output model."""

    entry_id: str = Field(..., description="Chart entry ID. REQUIRED for update operations.")
    case_id: str = Field(..., description="Parent case ID")
    action_type: str = Field(..., description="Action type (case_created|status_updated|etc.)")
    actor_persona: str = Field(..., description="Persona who performed the action")
    rationale: str | None = Field(None, description="Reason for the action")
    policy_refs: list[str] = Field(default_factory=list, description="Policy IDs referenced")
    evidence_links: list[str] = Field(default_factory=list, description="Evidence URLs")
    details: dict | None = Field(None, description="Action-specific details")
    timestamp: str = Field(..., description="Action timestamp")


class PolicyRefOutput(BaseModel):
    """Policy reference output model."""

    policy_id: str = Field(..., description="Unique policy identifier")
    country: str = Field(..., description="ISO country code. Default: US.")
    role: str | None = Field(None, description="Applicable role")
    employment_type: str | None = Field(None, description="Applicable employment type")
    policy_type: str = Field(
        ..., description="Policy type (prerequisites|lead_times|payroll_cutoffs|constraints)"
    )
    lead_time_days: int | None = Field(None, description="Lead time in days if applicable")
    content: dict = Field(..., description="Content data. Format depends on action.")
    effective_date: str = Field(..., description="Date change takes effect (YYYY-MM-DD). REQUIRED.")
    version: str = Field(..., description="Policy version")
    created_at: str = Field(..., description="Record creation timestamp")


class CaseDetailOutput(BaseModel):
    """Detailed case output with related data."""

    case: CaseOutput = Field(..., description="Case record")
    tasks: list[TaskOutput] | None = Field(None, description="Associated tasks")
    audit_trail: list[AuditEntryOutput] | None = Field(None, description="Audit history")
    policy_refs: list[PolicyRefOutput] = Field(
        default_factory=list, description="Attached policy references"
    )


class UpdateCaseStatusInput(BaseModel):
    """Input model for updating case status.

    Valid status transitions:
    - open -> in_progress
    - in_progress -> pending_approval, resolved
    - pending_approval -> approved, in_progress (rejection)
    - resolved -> closed
    - Any status -> closed (with hr_admin role)
    """

    case_id: str = Field(..., description="Case ID to update. Format: CASE-XXX (e.g., 'CASE-001').")
    new_status: Literal["open", "in_progress", "pending_approval", "resolved", "closed"] = Field(
        ...,
        description="New case status. Values: 'open', 'in_progress', 'pending_approval', 'resolved', 'closed'. Must be a valid transition from the current status.",
    )
    rationale: str = Field(
        ...,
        description="Reason for status change. Required for audit trail. Should explain why the transition is being made.",
    )
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description="Persona performing the action. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'. Some transitions require specific roles (e.g., closing requires hr_admin).",
    )


class AssignOwnerInput(BaseModel):
    """Input model for assigning case owner."""

    case_id: str = Field(
        ..., description="Case ID to reassign. Format: CASE-XXX (e.g., 'CASE-001')."
    )
    new_owner_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description="New owner persona. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )
    rationale: str = Field(..., description="Reason for reassignment. Required for audit trail.")
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description="Persona performing the reassignment. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )


class SearchCasesInput(BaseModel):
    """Input model for searching cases."""

    status: Literal["open", "in_progress", "pending_approval", "resolved", "closed"] | None = Field(
        None,
        description=(
            "Filter by case status. Options: open, in_progress, pending_approval, resolved, closed"
        ),
    )
    owner_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(None, description="Filter by owner persona")
    country: str | None = Field(None, description="ISO country code. Default: US.")
    role: str | None = Field(None, description="Filter by role")
    due_date_before: str | None = Field(None, description="Due date before (YYYY-MM-DD)")
    due_date_after: str | None = Field(None, description="Due date after (YYYY-MM-DD)")
    page_size: int = Field(default=50, ge=1, le=500, description="Results per page")
    page_number: int = Field(default=1, ge=1, description="Page number")


class SearchCasesOutput(BaseModel):
    """Output model for case search."""

    cases: list[CaseOutput] = Field(..., description="List of matching cases")
    total_count: int = Field(..., description="Total number of matching cases")
    page_size: int = Field(..., description="Results per page. Range: 1-100. Default: 25.")
    page_number: int = Field(..., description="Target page (1-indexed). REQUIRED.")


class HCMWorkerStateOutput(BaseModel):
    """HCM worker state output model."""

    worker_id: str = Field(..., description="Worker ID")
    case_id: str = Field(..., description="Associated case ID")
    onboarding_status: str | None = Field(
        None, description="Onboarding status (not_started|in_progress|ready|finalized)"
    )
    onboarding_readiness: bool = Field(..., description="Readiness flag")
    proposed_start_date: str | None = Field(None, description="Proposed start date")
    confirmed_start_date: str | None = Field(None, description="Confirmed start date")
    hire_finalized: bool = Field(..., description="Hire finalization flag")
    effective_date: str | None = Field(
        None, description="Date change takes effect (YYYY-MM-DD). REQUIRED."
    )
    created_at: str = Field(..., description="Record creation timestamp")
    updated_at: str = Field(..., description="Record update timestamp")


class HCMWriteLogOutput(BaseModel):
    """HCM write log output model."""

    log_id: str = Field(..., description="Unique log entry ID")
    case_id: str = Field(..., description="Associated case ID")
    worker_id: str = Field(..., description="Worker ID")
    write_type: str = Field(
        ..., description="Write type (confirm_start_date|update_readiness|finalize_hire)"
    )
    old_value: dict | None = Field(None, description="Previous value (JSON)")
    new_value: dict = Field(..., description="New value (JSON)")
    actor_persona: str = Field(..., description="Persona who performed the write")
    policy_refs: list[str] = Field(..., description="Policy IDs justifying the write")
    milestone_evidence: list[str] = Field(..., description="Milestone evidence links")
    rationale: str = Field(..., description="Write rationale")
    timestamp: str = Field(..., description="Write timestamp")


class CaseSnapshotInput(BaseModel):
    """Input model for case snapshot."""

    case_id: str = Field(..., description="Case ID")
    as_of_date: str | None = Field(None, description="Point-in-time snapshot (YYYY-MM-DD)")


class CaseSnapshotOutput(BaseModel):
    """Output model for complete case snapshot."""

    case: CaseDetailOutput = Field(..., description="Full case details")
    policy_references: list[PolicyRefOutput] = Field(
        ..., description="All attached policy references"
    )
    hcm_state: HCMWorkerStateOutput | None = Field(None, description="HCM state if exists")
    hcm_write_history: list[HCMWriteLogOutput] = Field(
        default_factory=list, description="HCM write history"
    )
    snapshot_timestamp: str = Field(..., description="Snapshot generation timestamp")


# =============================================================================
# V2 MILESTONE & TASK MODELS
# =============================================================================


class ListMilestonesInput(BaseModel):
    """Input model for listing milestones."""

    case_id: str = Field(..., description="Case ID")


class MilestoneListOutput(BaseModel):
    """Output model for milestone list."""

    milestones: list[MilestoneOutput] = Field(..., description="List of milestones")
    total_count: int = Field(..., description="Total number of milestones")
    completed_count: int = Field(..., description="Completed milestone count")
    pending_count: int = Field(..., description="Pending milestone count")


class UpdateMilestoneInput(BaseModel):
    """Input model for updating a milestone.

    Valid status transitions:
    - pending -> in_progress, waived, blocked
    - in_progress -> completed, blocked
    - blocked -> in_progress, waived
    - waived/completed are terminal states
    """

    case_id: str = Field(
        ..., description="Case ID containing the milestone. Format: CASE-XXX (e.g., 'CASE-001')."
    )
    milestone_type: Literal["screening", "work_authorization", "documents", "approvals"] = Field(
        ...,
        description="Milestone type to update. Values: 'screening' (background check), 'work_authorization' (visa/work permit), 'documents' (required paperwork), 'approvals' (management sign-offs).",
    )
    new_status: Literal["pending", "in_progress", "completed", "waived", "blocked"] = Field(
        ...,
        description="New milestone status. Values: 'pending' (not started), 'in_progress' (being worked on), 'completed' (finished - requires evidence_link), 'waived' (bypassed via approved exception), 'blocked' (cannot proceed).",
    )
    evidence_link: str | None = Field(
        None,
        description="URL or reference to evidence (e.g., 'https://docs.company.com/bgcheck/123'). Required when setting status to 'completed'.",
    )
    notes: str | None = Field(None, description="Additional notes for audit trail purposes.")
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description="Persona performing the update. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )


class CreateTaskInput(BaseModel):
    """Input model for creating a task."""

    case_id: str = Field(
        ..., description="Case ID to create task for. Format: CASE-XXX (e.g., 'CASE-001')."
    )
    milestone_type: str | None = Field(
        None,
        description="Optional milestone to link this task to. Values: 'screening', 'work_authorization', 'documents', 'approvals'. Null for standalone tasks.",
    )
    title: str = Field(
        ...,
        description="Brief task description (e.g., 'Complete I-9 verification', 'Schedule orientation'). Max 255 characters.",
    )
    owner_persona: str = Field(
        ...,
        description="Persona responsible for this task. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )
    due_date: str | None = Field(
        None,
        description="Task due date in YYYY-MM-DD format (e.g., '2024-02-28'). Null if no deadline.",
    )
    notes: str | None = Field(None, description="Additional notes for audit trail purposes.")


class UpdateTaskInput(BaseModel):
    """Input model for updating a task."""

    task_id: str = Field(..., description="Task ID to update")
    new_status: Literal["pending", "in_progress", "completed", "cancelled"] | None = Field(
        None,
        description="New task status. Options: pending, in_progress, completed, cancelled",
    )
    new_owner_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(None, description="New owner persona")
    notes: str | None = Field(None, description="Additional notes. Useful for audit trail.")
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(..., description="Persona performing the update")


# =============================================================================
# V2 POLICY MODELS
# =============================================================================


class GetApplicablePoliciesInput(BaseModel):
    """Input model for retrieving applicable policies."""

    country: str = Field(..., description="ISO country code. Default: US.")
    role: str | None = Field(None, description="Role/job title filter")
    employment_type: Literal["full_time", "part_time", "contractor"] | None = Field(
        None, description="Employment type filter. Options: full_time, part_time, contractor"
    )
    policy_type: Literal["prerequisites", "lead_times", "payroll_cutoffs", "constraints"] | None = (
        Field(
            None,
            description=(
                "Filter by policy type. "
                "Options: prerequisites, lead_times, payroll_cutoffs, constraints"
            ),
        )
    )
    as_of_date: str | None = Field(None, description="Effective as of date (YYYY-MM-DD)")


class ApplicablePoliciesOutput(BaseModel):
    """Output model for applicable policies."""

    policies: list[PolicyRefOutput] = Field(..., description="List of applicable policies")
    total_count: int = Field(..., description="Total number of policies")
    query_context: dict = Field(..., description="Echo back query parameters")


class AttachPolicyInput(BaseModel):
    """Input model for attaching policies to a case."""

    case_id: str = Field(..., description="Case ID")
    policy_ids: list[str] = Field(..., description="Policy IDs to attach")
    decision_context: str = Field(..., description="Why these policies are relevant")
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description=(
            "Persona attaching policies. "
            "Options: pre_onboarding_coordinator, hr_admin, hr_business_partner, "
            "hiring_manager, auditor"
        ),
    )


class CasePolicyLinkOutput(BaseModel):
    """Case-policy link output model."""

    link_id: str = Field(..., description="Unique link identifier")
    case_id: str = Field(..., description="Case ID")
    policy_id: str = Field(..., description="Policy ID")
    attached_at: str = Field(..., description="Attachment timestamp")
    attached_by: str = Field(..., description="Persona who attached")
    decision_context: str | None = Field(None, description="Context for attachment")


class CreatePolicyInput(BaseModel):
    """Input model for creating a policy reference."""

    policy_id: str = Field(
        ...,
        description="Unique policy identifier. Format: POLICY-{COUNTRY}-{TYPE}-{SEQUENCE} (e.g., 'POLICY-US-LEAD-TIME-001'). User-provided, must be unique.",
    )
    country: str = Field(
        ...,
        description="ISO 3166-1 alpha-2 country code (e.g., 'US', 'GB', 'DE'). Use uppercase two-letter codes.",
    )
    policy_type: Literal["prerequisites", "lead_times", "payroll_cutoffs", "constraints"] = Field(
        ...,
        description="Type of policy. Values: 'prerequisites' (required milestones), 'lead_times' (minimum days before start), 'payroll_cutoffs' (payroll processing deadlines), 'constraints' (other restrictions).",
    )
    content: dict = Field(
        ...,
        description='Policy details as a JSON object. Structure depends on policy_type. Example for lead_times: {"min_days": 14, "description": "US standard 2-week notice"}. Example for prerequisites: {"required_milestones": ["screening", "work_authorization"]}.',
    )
    effective_date: str = Field(
        ...,
        description="Date the policy takes effect in YYYY-MM-DD format (e.g., '2024-01-01'). Policies are matched based on this date.",
    )
    version: str = Field(
        ...,
        description="Policy version for tracking changes (e.g., '1.0', '2.1'). Semantic versioning recommended.",
    )
    role: str | None = Field(
        None, description="Applicable role filter. Null means policy applies to all roles."
    )
    employment_type: str | None = Field(
        None,
        description="Applicable employment type filter. Values: 'full_time', 'part_time', 'contractor'. Null means policy applies to all types.",
    )
    lead_time_days: int | None = Field(
        None,
        description="Minimum lead time in calendar days. Required for policy_type='lead_times'. Example: 14 means 14 days notice required before start date.",
    )

    @field_validator("content", mode="before")
    @classmethod
    def parse_content_json(cls, v: dict | str) -> dict:
        """Parse content from JSON string if provided as string."""
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if not isinstance(parsed, dict):
                    raise ValueError("Content must be a JSON object, not an array or primitive")
                return parsed
            except json.JSONDecodeError as e:
                raise ValueError(f"Content must be valid JSON: {e}") from e
        raise ValueError("Content must be a dict or JSON string")


class CreatePayrollCutoffInput(BaseModel):
    """Input model for creating a payroll cutoff rule."""

    cutoff_id: str = Field(
        ...,
        description="Unique cutoff identifier. Format: CUTOFF-{COUNTRY}-{SEQUENCE} (e.g., 'CUTOFF-US-001'). User-provided, must be unique.",
    )
    country: str = Field(
        ...,
        description="ISO 3166-1 alpha-2 country code (e.g., 'US', 'GB', 'DE'). Use uppercase two-letter codes.",
    )
    cutoff_day_of_month: int = Field(
        ...,
        ge=1,
        le=31,
        description="Day of month for payroll cutoff (1-31). New hires must be entered before this day to be included in that month's payroll.",
    )
    processing_days: int = Field(
        ...,
        ge=1,
        description="Number of business days required for payroll processing before cutoff. Example: 3 means new hire data must be entered 3 business days before cutoff_day_of_month.",
    )
    effective_date: str = Field(
        ...,
        description="Date this cutoff rule takes effect in YYYY-MM-DD format (e.g., '2024-01-01').",
    )


# =============================================================================
# V2 HCM CONTEXT MODELS
# =============================================================================


class ReadHCMContextInput(BaseModel):
    """Input model for reading HCM context."""

    case_id: str = Field(..., description="Case ID")


class HCMContextOutput(BaseModel):
    """Output model for HCM context."""

    case_id: str = Field(..., description="Case ID")
    worker_id: str | None = Field(None, description="Worker ID if assigned")
    onboarding_status: str | None = Field(
        None, description="Onboarding status (not_started|in_progress|ready|finalized)"
    )
    onboarding_readiness: bool = Field(..., description="Readiness flag")
    proposed_start_date: str | None = Field(None, description="Proposed start date")
    confirmed_start_date: str | None = Field(None, description="Confirmed start date")
    hire_finalized: bool = Field(..., description="Hire finalized flag")
    last_updated: str | None = Field(None, description="Last update timestamp")


class ReadPositionInput(BaseModel):
    """Input model for reading position context."""

    case_id: str = Field(..., description="Case ID")


class PositionContextOutput(BaseModel):
    """Output model for position context with derived policy requirements."""

    case_id: str = Field(..., description="Case ID")
    role: str = Field(..., description="Job role")
    country: str = Field(..., description="ISO country code. Default: US.")
    employment_type: str = Field(..., description="Employment type")
    required_milestones: list[str] = Field(..., description="Required milestone types")
    minimum_lead_time_days: int | None = Field(None, description="Minimum lead time from policy")
    payroll_cutoff_day: int | None = Field(None, description="Payroll cutoff day from policy")


# =============================================================================
# V2 GATED HCM WRITE-BACK MODELS
# =============================================================================


class GatingCheckResult(BaseModel):
    """Result of a single gating check."""

    check_name: str = Field(
        ..., description="Check name (milestones_complete|lead_time_valid|payroll_cutoff_valid)"
    )
    passed: bool = Field(..., description="Whether the check passed")
    details: str = Field(..., description="Details about the check result")


class ConfirmStartDateInput(BaseModel):
    """Input model for confirming start date (gated write-back).

    This is a gated operation that requires:
    1. All required milestones must be completed (or waived with approved exception)
    2. Lead time requirements must be satisfied
    3. Payroll cutoff constraints must be met
    """

    case_id: str = Field(
        ..., description="Case ID to confirm start date for. Format: CASE-XXX (e.g., 'CASE-001')."
    )
    confirmed_start_date: str = Field(
        ...,
        description="Start date to confirm in YYYY-MM-DD format (e.g., '2024-03-15'). Must satisfy lead time and payroll cutoff constraints.",
    )
    policy_refs: list[str] = Field(
        ...,
        description="List of policy IDs justifying this decision. Format: ['POLICY-US-001', 'POLICY-US-002']. At least one policy reference required.",
    )
    evidence_links: list[str] = Field(
        ...,
        description="List of URLs or references to evidence supporting the confirmation. Example: ['https://docs.company.com/approvals/123', 'MILESTONE-screening-complete']. At least one evidence link required.",
    )
    rationale: str = Field(
        ..., description="Free-text rationale explaining the decision. Required for audit trail."
    )
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description="Persona performing the write-back. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )


class ConfirmStartDateOutput(BaseModel):
    """Output model for start date confirmation."""

    success: bool = Field(..., description="Whether the confirmation succeeded")
    case_id: str = Field(..., description="Case ID")
    confirmed_start_date: str = Field(..., description="Confirmed start date")
    gating_checks: list[GatingCheckResult] = Field(..., description="Details of each check")
    hcm_write_id: str = Field(..., description="Reference to the write log entry")
    timestamp: str = Field(..., description="Confirmation timestamp")


class UpdateReadinessInput(BaseModel):
    """Input model for updating onboarding readiness flag."""

    case_id: str = Field(
        ..., description="Case ID to update readiness for. Format: CASE-XXX (e.g., 'CASE-001')."
    )
    onboarding_readiness: bool = Field(
        ...,
        description="Readiness flag value. True = ready for onboarding, False = not ready. Setting to True triggers HCM write-back.",
    )
    policy_refs: list[str] = Field(
        ...,
        description="List of policy IDs justifying this update. Format: ['POLICY-US-001', 'POLICY-US-002']. At least one policy reference required.",
    )
    evidence_links: list[str] = Field(
        ...,
        description="List of URLs or references to evidence supporting the update. Example: ['https://docs.company.com/approvals/123', 'MILESTONE-screening-complete']. At least one evidence link required.",
    )
    rationale: str = Field(
        ..., description="Rationale explaining the readiness decision. Required for audit trail."
    )
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description="Persona performing the update. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )


class UpdateReadinessOutput(BaseModel):
    """Output model for readiness update."""

    success: bool = Field(..., description="Whether the update succeeded")
    case_id: str = Field(..., description="Case ID")
    onboarding_readiness: bool = Field(..., description="New readiness value")
    hcm_write_id: str = Field(..., description="Reference to the write log entry")
    timestamp: str = Field(..., description="Update timestamp")


# =============================================================================
# V2 EXCEPTION MODELS
# =============================================================================


class ExceptionOutput(BaseModel):
    """Exception request output model."""

    exception_id: str = Field(..., description="Unique exception identifier")
    case_id: str = Field(..., description="Case ID")
    milestone_type: str = Field(..., description="Milestone requiring exception")
    reason: str = Field(..., description="Explanation for the action. REQUIRED for adjustments.")
    affected_policy_refs: list[str] = Field(
        default_factory=list, description="Policies being excepted"
    )
    requested_by: str = Field(..., description="Persona who requested")
    requested_at: str = Field(..., description="Request timestamp")
    approval_status: str = Field(..., description="Status (pending|approved|denied)")
    approved_by: str | None = Field(None, description="Persona who approved")
    approval_notes: str | None = Field(None, description="Approval notes")
    approved_at: str | None = Field(None, description="Approval timestamp")


class RequestExceptionInput(BaseModel):
    """Input model for requesting an exception."""

    case_id: str = Field(..., description="Case ID")
    milestone_type: Literal["screening", "work_authorization", "documents", "approvals"] = Field(
        ...,
        description=(
            "Milestone requiring exception. "
            "Options: screening, work_authorization, documents, approvals"
        ),
    )
    reason: str = Field(..., description="Explanation for the action. REQUIRED for adjustments.")
    affected_policy_refs: list[str] = Field(
        default_factory=list, description="Policies being excepted"
    )
    actor_persona: Literal[
        "pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
    ] = Field(
        ...,
        description=(
            "Persona requesting the exception. "
            "Options: pre_onboarding_coordinator, hr_admin, hr_business_partner, "
            "hiring_manager, auditor"
        ),
    )


class ApproveExceptionInput(BaseModel):
    """Input model for approving/denying an exception."""

    exception_id: str = Field(..., description="Exception ID to approve")
    approval_status: Literal["approved", "denied"] = Field(
        ..., description="Approval decision. Options: approved, denied"
    )
    approval_notes: str = Field(..., description="Mandatory notes explaining decision")
    actor_persona: Literal["hr_admin"] = Field(..., description="Must be hr_admin")


# =============================================================================
# V2 AUDIT MODELS
# =============================================================================


class GetAuditHistoryInput(BaseModel):
    """Input model for retrieving audit history."""

    case_id: str = Field(..., description="Case ID")
    action_type: str | None = Field(None, description="Filter by action type")
    actor_persona: str | None = Field(None, description="Filter by actor")
    start_date: str | None = Field(
        None, description="Range start (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )
    end_date: str | None = Field(
        None, description="Range end (YYYY-MM-DD). REQUIRED for date-bounded queries."
    )


class AuditHistoryOutput(BaseModel):
    """Output model for audit history."""

    entries: list[AuditEntryOutput] = Field(..., description="Audit entries")
    total_count: int = Field(..., description="Total number of entries")


# =============================================================================
# V2 HEALTH CHECK MODEL
# =============================================================================


class HealthCheckOutput(BaseModel):
    """Output model for health check."""

    status: str = Field(
        ...,
        description="Health check status. Values: 'healthy' (all systems operational), 'degraded' (partial issues), 'unhealthy' (critical issues).",
    )
    database_connected: bool = Field(
        ..., description="Database connectivity status. True if connected."
    )
    version: str = Field(..., description="Server version string (e.g., '1.0.0').")
    uptime_seconds: int = Field(..., description="Server uptime in seconds since last restart.")
    timestamp: str = Field(
        ...,
        description="Health check timestamp in ISO 8601 format with timezone (e.g., '2024-01-15T14:30:00Z').",
    )


# =============================================================================
# V2 FOUNDATION SETUP MODELS (Extensions)
# =============================================================================


class CreateCostCenterInput(BaseModel):
    """Input model for creating a cost center."""

    cost_center_id: str = Field(..., description="Unique cost center identifier")
    cost_center_name: str = Field(..., description="Cost center display name")
    org_id: str = Field(..., description="Associated organization ID")


class CostCenterOutput(BaseModel):
    """Cost center output model."""

    cost_center_id: str = Field(..., description="Cost center ID")
    cost_center_name: str = Field(..., description="Cost center name")
    org_id: str = Field(..., description="Associated organization ID")
    created_at: str = Field(..., description="Record creation timestamp")


class CreateLocationInput(BaseModel):
    """Input model for creating a location."""

    location_id: str = Field(..., description="Unique location identifier")
    location_name: str = Field(..., description="Location display name")
    city: str | None = Field(
        None, description="City name. Improves tax accuracy for multi-zone cities."
    )
    country: str = Field(..., description="ISO country code. Default: US.")


class LocationOutput(BaseModel):
    """Location output model."""

    location_id: str = Field(..., description="Location ID")
    location_name: str = Field(..., description="Location name")
    city: str | None = Field(
        None, description="City name. Improves tax accuracy for multi-zone cities."
    )
    country: str = Field(..., description="ISO country code. Default: US.")
    created_at: str = Field(..., description="Record creation timestamp")


class PayrollCutoffOutput(BaseModel):
    """Payroll cutoff output model."""

    cutoff_id: str = Field(..., description="Cutoff ID")
    country: str = Field(..., description="ISO country code. Default: US.")
    cutoff_day_of_month: int = Field(..., description="Day of month for cutoff")
    processing_days: int = Field(..., description="Processing days after cutoff")
    effective_date: str = Field(..., description="Date change takes effect (YYYY-MM-DD). REQUIRED.")
    created_at: str = Field(..., description="Record creation timestamp")
