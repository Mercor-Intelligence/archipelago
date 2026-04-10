"""Meta-tool input/output schemas for Workday HCM MCP server.

This module contains all Pydantic models for meta-tools:
- HelpResponse: Standard help response structure
- MetaToolOutput: Standard output wrapper for all meta-tools
- Input models for each meta-tool domain (WorkersInput, PositionsInput, etc.)
- TOOL_SCHEMAS: Registry for schema introspection
"""

from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field

# =============================================================================
# Base Models
# =============================================================================


class HelpResponse(BaseModel):
    """Standard help response structure for meta-tools."""

    tool_name: str = Field(
        ..., description="Meta-tool name (e.g., 'workday_workers', 'workday_cases')."
    )
    description: str = Field(..., description="Human-readable description of the tool's purpose.")
    actions: dict[str, dict[str, Any]] = Field(
        ...,
        description="Available actions mapped to their parameters and descriptions. Keys are action names (e.g., 'create', 'get'), values contain 'description' and 'parameters' keys.",
    )


class MetaToolOutput(BaseModel):
    """Standard output wrapper for all meta-tools."""

    action: str = Field(
        ..., description="The operation that was performed (echoes request action)."
    )
    help: HelpResponse | None = Field(
        None, description="Help documentation. Only populated when action='help'."
    )
    data: dict[str, Any] | None = Field(
        None,
        description="Operation result data. Structure depends on the action performed. Contains the created/retrieved/updated record(s).",
    )
    error: str | None = Field(
        None, description="Error message if the operation failed. Null on success."
    )


# =============================================================================
# Workers Meta-Tool Models
# =============================================================================


class WorkersInput(BaseModel):
    """Input model for workday_workers meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "hire", "get", "list", "transfer", "terminate"] = Field(
        ..., description="Action: 'help', 'hire', 'get', 'list', 'transfer', 'terminate'"
    )

    # Common identifier
    worker_id: str | None = Field(  # noqa: N815
        default=None,
        alias="workerId",
        description="Worker ID. REQUIRED for get/transfer/terminate. Format: WRK-XXXXX.",
    )

    # Hire action fields
    job_profile_id: str | None = Field(  # noqa: N815
        default=None,
        alias="jobProfileId",
        description="Job profile ID (required for hire). Format: alphanumeric string (e.g., 'JP-ENG-002').",
    )
    org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="orgId",
        description="Supervisory organization ID (required for hire). Format: alphanumeric string (e.g., 'ORG-001').",
    )
    cost_center_id: str | None = Field(  # noqa: N815
        default=None,
        alias="costCenterId",
        description="Cost center ID (required for hire). Format: alphanumeric string (e.g., 'CC-001').",
    )
    location_id: str | None = Field(  # noqa: N815
        default=None,
        alias="locationId",
        description="Location ID (optional for hire). Format: alphanumeric string (e.g., 'LOC-NYC-001').",
    )
    position_id: str | None = Field(  # noqa: N815
        default=None,
        alias="positionId",
        description="Position ID to fill (optional for hire, transfer). Format: POS-XXXXX (e.g., 'POS-00456').",
    )
    fte: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Full-time equivalent. 1.0=full-time, 0.5=half-time. Range: 0.0-1.0.",
    )
    hire_date: str | None = Field(  # noqa: N815
        default=None,
        alias="hireDate",
        description="Hire date. REQUIRED for hire. Format: YYYY-MM-DD (e.g., 2024-01-15).",
    )

    # Terminate action fields
    termination_date: str | None = Field(  # noqa: N815
        default=None,
        alias="terminationDate",
        description="Termination date (required for terminate). Format: YYYY-MM-DD (e.g., '2024-03-15').",
    )

    # Transfer action fields
    new_org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="newOrgId",
        description="New organization ID (optional for transfer). Format: alphanumeric string (e.g., 'ORG-002').",
    )
    new_cost_center_id: str | None = Field(  # noqa: N815
        default=None,
        alias="newCostCenterId",
        description="New cost center ID (optional for transfer). Format: alphanumeric string (e.g., 'CC-002').",
    )
    new_job_profile_id: str | None = Field(  # noqa: N815
        default=None,
        alias="newJobProfileId",
        description="New job profile ID (optional for transfer). Format: alphanumeric string (e.g., 'JP-ENG-003').",
    )
    new_position_id: str | None = Field(  # noqa: N815
        default=None,
        alias="newPositionId",
        description="New position ID (optional for transfer). Worker's current position is released. Format: POS-XXXXX (e.g., 'POS-00789').",
    )
    new_fte: float | None = Field(  # noqa: N815
        default=None,
        alias="newFte",
        ge=0.0,
        le=1.0,
        description="New FTE value (optional for transfer). Range: 0.0-1.0 where 1.0=full-time.",
    )
    transfer_date: str | None = Field(  # noqa: N815
        default=None,
        alias="transferDate",
        description="Transfer date (required for transfer). Format: YYYY-MM-DD (e.g., '2024-03-15').",
    )

    # Shared optional fields
    effective_date: str | None = Field(  # noqa: N815
        default=None,
        alias="effectiveDate",
        description="Date the change takes effect in Workday systems. Format: YYYY-MM-DD (e.g., '2024-03-15'). Defaults to action date if not provided.",
    )
    as_of_date: str | None = Field(  # noqa: N815
        default=None,
        alias="asOfDate",
        description="Point-in-time query date (for get, list). Format: YYYY-MM-DD (e.g., '2024-01-15'). Returns state as of that date.",
    )

    # List action fields
    page_size: int | None = Field(  # noqa: N815
        default=None,
        alias="pageSize",
        ge=1,
        le=1000,
        description="Number of results per page. Range: 1-1000. Default: 100.",
    )
    page_number: int | None = Field(  # noqa: N815
        default=None,
        alias="pageNumber",
        ge=1,
        description="Page number to retrieve (1-indexed). Default: 1.",
    )
    employment_status: Literal["Active", "Terminated", "Leave"] | None = Field(  # noqa: N815
        default=None,
        alias="employmentStatus",
        description="Filter by employment status (for list). Values: 'Active', 'Terminated', 'Leave'.",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Positions Meta-Tool Models
# =============================================================================


class PositionsInput(BaseModel):
    """Input model for workday_positions meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "create", "get", "list", "close"] = Field(
        ..., description="Action: 'help', 'create', 'get', 'list', 'close'"
    )

    # Common identifier
    position_id: str | None = Field(  # noqa: N815
        default=None,
        alias="positionId",
        description="Position ID (required for get, close)",
    )

    # Create action fields
    job_profile_id: str | None = Field(  # noqa: N815
        default=None,
        alias="jobProfileId",
        description="Job profile ID (required for create)",
    )
    org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="orgId",
        description="Supervisory organization ID (required for create)",
    )
    fte: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="FTE allocation 0.0-1.0 (default 1.0)",
    )

    # List action fields
    status: Literal["open", "filled", "closed"] | None = Field(
        default=None,
        description="Filter by position status (for list)",
    )
    page_size: int | None = Field(  # noqa: N815
        default=None,
        alias="pageSize",
        ge=1,
        le=1000,
        description="Results per page (default 100, max 1000)",
    )
    page_number: int | None = Field(  # noqa: N815
        default=None,
        alias="pageNumber",
        ge=1,
        description="Page number (default 1)",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Organizations Meta-Tool Models
# =============================================================================


class OrganizationsInput(BaseModel):
    """Input model for workday_organizations meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal[
        "help", "create", "get", "list", "hierarchy", "create_cost_center", "create_location"
    ] = Field(
        ...,
        description=(
            "Action: 'help', 'create', 'get', 'list', 'hierarchy', "
            "'create_cost_center', 'create_location'"
        ),
    )

    # Organization fields
    org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="orgId",
        description="Organization ID (required for create, get; optional for list, hierarchy)",
    )
    org_name: str | None = Field(  # noqa: N815
        default=None,
        alias="orgName",
        description="Organization name (required for create)",
    )
    org_type: Literal["Supervisory", "Cost_Center", "Location"] | None = Field(  # noqa: N815
        default=None,
        alias="orgType",
        description="Organization type (default: Supervisory)",
    )
    parent_org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="parentOrgId",
        description="Parent organization ID (optional for create, list filter)",
    )
    manager_worker_id: str | None = Field(  # noqa: N815
        default=None,
        alias="managerWorkerId",
        description="Manager worker ID (optional for create)",
    )
    root_org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="rootOrgId",
        description="Root org ID for hierarchy (optional, returns full tree if None)",
    )
    root_only: bool | None = Field(  # noqa: N815
        default=None,
        alias="rootOnly",
        description="Filter for root organizations only (for list)",
    )

    # Cost center fields
    cost_center_id: str | None = Field(  # noqa: N815
        default=None,
        alias="costCenterId",
        description="Cost center ID (required for create_cost_center)",
    )
    cost_center_name: str | None = Field(  # noqa: N815
        default=None,
        alias="costCenterName",
        description="Cost center name (required for create_cost_center)",
    )

    # Location fields
    location_id: str | None = Field(  # noqa: N815
        default=None,
        alias="locationId",
        description="Location ID (required for create_location)",
    )
    location_name: str | None = Field(  # noqa: N815
        default=None,
        alias="locationName",
        description="Location name (required for create_location)",
    )
    city: str | None = Field(
        default=None,
        description="City name (optional for create_location)",
    )
    country: str | None = Field(
        default=None,
        description="Country code (required for create_location)",
    )

    # Pagination
    page_size: int | None = Field(  # noqa: N815
        default=None,
        alias="pageSize",
        ge=1,
        le=1000,
        description="Results per page (default 100, max 1000)",
    )
    page_number: int | None = Field(  # noqa: N815
        default=None,
        alias="pageNumber",
        ge=1,
        description="Page number (default 1)",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Job Profiles Meta-Tool Models
# =============================================================================


class JobProfilesInput(BaseModel):
    """Input model for workday_job_profiles meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "create", "get", "list"] = Field(
        ..., description="Action: 'help', 'create', 'get', 'list'"
    )

    # Common identifier
    job_profile_id: str | None = Field(  # noqa: N815
        default=None,
        alias="jobProfileId",
        description="Job profile ID (required for create, get)",
    )

    # Create fields
    title: str | None = Field(
        default=None,
        description="Job title (required for create)",
    )
    job_family: str | None = Field(  # noqa: N815
        default=None,
        alias="jobFamily",
        description="Job family (required for create, optional for list filter)",
    )
    job_level: str | None = Field(  # noqa: N815
        default=None,
        alias="jobLevel",
        description="Job level (optional for create)",
    )

    # Pagination
    page_size: int | None = Field(  # noqa: N815
        default=None,
        alias="pageSize",
        ge=1,
        le=1000,
        description="Results per page (default 100, max 1000)",
    )
    page_number: int | None = Field(  # noqa: N815
        default=None,
        alias="pageNumber",
        ge=1,
        description="Page number (default 1)",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Cases Meta-Tool Models
# =============================================================================


class CasesInput(BaseModel):
    """Input model for workday_cases meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "create", "get", "update", "assign_owner", "search", "snapshot"] = (
        Field(
            ...,
            description=(
                "Action: 'help', 'create', 'get', 'update', 'assign_owner', 'search', 'snapshot'"
            ),
        )
    )

    # Common identifier
    case_id: str | None = Field(  # noqa: N815
        default=None,
        alias="caseId",
        description="Case ID. Format: CASE-XXX (e.g., 'CASE-001').",
    )

    # Create fields
    candidate_id: str | None = Field(  # noqa: N815
        default=None,
        alias="candidateId",
        description="Candidate identifier from ATS (required for create). Format: alphanumeric string (e.g., 'CAND-12345').",
    )
    requisition_id: str | None = Field(  # noqa: N815
        default=None,
        alias="requisitionId",
        description="Requisition/job opening ID. Format: alphanumeric string (e.g., 'REQ-2024-001').",
    )
    role: str | None = Field(
        default=None,
        description="Job role/title (required for create). Example: 'Software Engineer', 'Product Manager'.",
    )
    country: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code (required for create). Use uppercase two-letter codes (e.g., 'US', 'GB', 'DE').",
    )
    employment_type: Literal["full_time", "part_time", "contractor"] | None = Field(  # noqa: N815
        default=None,
        alias="employmentType",
        description="Employment type. Values: 'full_time', 'part_time', 'contractor'. Default: 'full_time'.",
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
    ) = Field(  # noqa: N815
        default=None,
        alias="ownerPersona",
        description="Case owner persona. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )
    proposed_start_date: str | None = Field(  # noqa: N815
        default=None,
        alias="proposedStartDate",
        description="Initial proposed start date. Format: YYYY-MM-DD (e.g., '2024-03-15'). Must be a future date.",
    )
    due_date: str | None = Field(  # noqa: N815
        default=None,
        alias="dueDate",
        description="Case deadline. Format: YYYY-MM-DD (e.g., '2024-02-28'). Should be before proposed_start_date.",
    )
    notes: str | None = Field(
        default=None,
        description="Additional notes for audit trail purposes.",
    )

    # Update fields
    new_status: (
        Literal[  # noqa: N815
            "open", "in_progress", "pending_approval", "resolved", "closed"
        ]
        | None
    ) = Field(
        default=None,
        alias="newStatus",
        description="New case status (for update). Values: 'open', 'in_progress', 'pending_approval', 'resolved', 'closed'. Must be a valid transition.",
    )
    rationale: str | None = Field(
        default=None,
        description="Reason for status change or reassignment. Required for audit trail.",
    )
    actor_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(  # noqa: N815
        default=None,
        alias="actorPersona",
        description="Persona performing the action. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )

    # Assign owner fields
    new_owner_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(  # noqa: N815
        default=None,
        alias="newOwnerPersona",
        description="New owner persona (for assign_owner)",
    )

    # Get fields
    include_tasks: bool | None = Field(  # noqa: N815
        default=None,
        alias="includeTasks",
        description="Include associated tasks (for get)",
    )
    include_audit: bool | None = Field(  # noqa: N815
        default=None,
        alias="includeAudit",
        description="Include audit trail (for get)",
    )

    # Search fields
    status: Literal["open", "in_progress", "pending_approval", "resolved", "closed"] | None = Field(
        default=None,
        description="Filter by case status (for search)",
    )
    due_date_before: str | None = Field(  # noqa: N815
        default=None,
        alias="dueDateBefore",
        description="Due date before YYYY-MM-DD (for search)",
    )
    due_date_after: str | None = Field(  # noqa: N815
        default=None,
        alias="dueDateAfter",
        description="Due date after YYYY-MM-DD (for search)",
    )

    # Snapshot fields
    as_of_date: str | None = Field(  # noqa: N815
        default=None,
        alias="asOfDate",
        description="Point-in-time snapshot date YYYY-MM-DD",
    )

    # Pagination
    page_size: int | None = Field(  # noqa: N815
        default=None,
        alias="pageSize",
        ge=1,
        le=500,
        description="Results per page (default 50, max 500)",
    )
    page_number: int | None = Field(  # noqa: N815
        default=None,
        alias="pageNumber",
        ge=1,
        description="Page number (default 1)",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# HCM Meta-Tool Models
# =============================================================================


class HCMInput(BaseModel):
    """Input model for workday_hcm meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal[
        "help", "read_context", "read_position", "confirm_start_date", "update_readiness"
    ] = Field(
        ...,
        description=(
            "Action: 'help', 'read_context', 'read_position', "
            "'confirm_start_date', 'update_readiness'"
        ),
    )

    # Common identifier
    case_id: str | None = Field(  # noqa: N815
        default=None,
        alias="caseId",
        description="Case ID (required for all actions except help)",
    )

    # Confirm start date fields
    confirmed_start_date: str | None = Field(  # noqa: N815
        default=None,
        alias="confirmedStartDate",
        description="Start date to confirm. Format: YYYY-MM-DD (e.g., '2024-03-15'). Must satisfy lead time and payroll constraints.",
    )
    policy_refs: list[str] | None = Field(  # noqa: N815
        default=None,
        alias="policyRefs",
        description="Policy IDs justifying the decision. Format: ['POLICY-US-001', 'POLICY-US-002']. At least one required.",
    )
    evidence_links: list[str] | None = Field(  # noqa: N815
        default=None,
        alias="evidenceLinks",
        description="URLs/references to evidence. Example: ['https://docs.company.com/approvals/123', 'MILESTONE-screening-complete']. At least one required.",
    )
    rationale: str | None = Field(
        default=None,
        description="Free-text rationale explaining the decision. Required for audit trail.",
    )
    actor_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(  # noqa: N815
        default=None,
        alias="actorPersona",
        description="Persona performing the action. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )

    # Update readiness fields
    onboarding_readiness: bool | None = Field(  # noqa: N815
        default=None,
        alias="onboardingReadiness",
        description="Readiness flag. True = ready for onboarding (triggers HCM write-back), False = not ready.",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Milestones Meta-Tool Models
# =============================================================================


class MilestonesInput(BaseModel):
    """Input model for workday_milestones meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "list", "update"] = Field(
        ..., description="Action: 'help', 'list', 'update'"
    )

    # Common identifier
    case_id: str | None = Field(  # noqa: N815
        default=None,
        alias="caseId",
        description="Case ID (required for list, update)",
    )

    # Update fields
    milestone_type: (
        Literal[  # noqa: N815
            "screening", "work_authorization", "documents", "approvals"
        ]
        | None
    ) = Field(
        default=None,
        alias="milestoneType",
        description="Milestone type to update. Values: 'screening' (background check), 'work_authorization' (visa/work permit), 'documents' (required paperwork), 'approvals' (management sign-offs).",
    )
    new_status: Literal["pending", "in_progress", "completed", "waived", "blocked"] | None = Field(  # noqa: N815
        default=None,
        alias="newStatus",
        description="New milestone status. Values: 'pending', 'in_progress', 'completed' (requires evidence_link), 'waived' (exception approved), 'blocked'.",
    )
    evidence_link: str | None = Field(  # noqa: N815
        default=None,
        alias="evidenceLink",
        description="URL or reference to evidence (e.g., 'https://docs.company.com/bgcheck/123'). Required when setting status to 'completed'.",
    )
    notes: str | None = Field(
        default=None,
        description="Notes about the update for audit trail purposes.",
    )
    actor_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(  # noqa: N815
        default=None,
        alias="actorPersona",
        description="Persona performing the update. Values: 'pre_onboarding_coordinator', 'hr_admin', 'hr_business_partner', 'hiring_manager', 'auditor'.",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Tasks Meta-Tool Models
# =============================================================================


class TasksInput(BaseModel):
    """Input model for workday_tasks meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "create", "update"] = Field(
        ..., description="Action: 'help', 'create', 'update'"
    )

    # Common identifiers
    task_id: str | None = Field(  # noqa: N815
        default=None,
        alias="taskId",
        description="Task ID (required for update)",
    )
    case_id: str | None = Field(  # noqa: N815
        default=None,
        alias="caseId",
        description="Case ID (required for create)",
    )

    # Create fields
    milestone_type: str | None = Field(  # noqa: N815
        default=None,
        alias="milestoneType",
        description="Optional milestone to link",
    )
    title: str | None = Field(
        default=None,
        description="Task title (required for create)",
    )
    owner_persona: str | None = Field(  # noqa: N815
        default=None,
        alias="ownerPersona",
        description="Task owner persona (required for create)",
    )
    due_date: str | None = Field(  # noqa: N815
        default=None,
        alias="dueDate",
        description="Task due date YYYY-MM-DD",
    )
    notes: str | None = Field(
        default=None,
        description="Task notes",
    )

    # Update fields
    new_status: Literal["pending", "in_progress", "completed", "cancelled"] | None = Field(  # noqa: N815
        default=None,
        alias="newStatus",
        description="New task status",
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
    ) = Field(  # noqa: N815
        default=None,
        alias="newOwnerPersona",
        description="New owner persona",
    )
    actor_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(  # noqa: N815
        default=None,
        alias="actorPersona",
        description="Persona performing the update",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Policies Meta-Tool Models
# =============================================================================


class PoliciesInput(BaseModel):
    """Input model for workday_policies meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal[
        "help", "get_applicable", "attach_to_case", "create", "create_payroll_cutoff"
    ] = Field(
        ...,
        description=(
            "Action: 'help', 'get_applicable', 'attach_to_case', 'create', 'create_payroll_cutoff'"
        ),
    )

    # Common fields
    country: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code. Use uppercase two-letter codes (e.g., 'US', 'GB', 'DE'). Required for get_applicable, create, create_payroll_cutoff.",
    )
    role: str | None = Field(
        default=None,
        description="Role/job title filter. Null means policy applies to all roles.",
    )
    employment_type: Literal["full_time", "part_time", "contractor"] | None = Field(  # noqa: N815
        default=None,
        alias="employmentType",
        description="Employment type filter. Values: 'full_time', 'part_time', 'contractor'. Null means policy applies to all types.",
    )
    policy_type: (
        Literal[  # noqa: N815
            "prerequisites", "lead_times", "payroll_cutoffs", "constraints"
        ]
        | None
    ) = Field(
        default=None,
        alias="policyType",
        description="Policy type. Values: 'prerequisites' (required milestones), 'lead_times' (minimum days before start), 'payroll_cutoffs' (payroll deadlines), 'constraints' (other restrictions).",
    )
    as_of_date: str | None = Field(  # noqa: N815
        default=None,
        alias="asOfDate",
        description="Effective as of date. Format: YYYY-MM-DD (e.g., '2024-01-15'). Policies are matched based on this date.",
    )

    # Attach to case fields
    case_id: str | None = Field(  # noqa: N815
        default=None,
        alias="caseId",
        description="Case ID (required for attach_to_case)",
    )
    policy_ids: list[str] | None = Field(  # noqa: N815
        default=None,
        alias="policyIds",
        description="Policy IDs to attach",
    )
    decision_context: str | None = Field(  # noqa: N815
        default=None,
        alias="decisionContext",
        description="Why these policies are relevant",
    )
    actor_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(  # noqa: N815
        default=None,
        alias="actorPersona",
        description="Persona attaching policies",
    )

    # Create policy fields
    policy_id: str | None = Field(  # noqa: N815
        default=None,
        alias="policyId",
        description="Unique policy identifier. Format: POLICY-{COUNTRY}-{TYPE}-{SEQ} (e.g., 'POLICY-US-LEAD-TIME-001').",
    )
    content: dict | None = Field(
        default=None,
        description='Policy details as JSON object. Structure depends on policy_type. Example for lead_times: {"min_days": 14, "description": "US standard 2-week notice"}.',
    )
    effective_date: str | None = Field(  # noqa: N815
        default=None,
        alias="effectiveDate",
        description="Policy effective date. Format: YYYY-MM-DD (e.g., '2024-01-01').",
    )
    version: str | None = Field(
        default=None,
        description="Policy version for tracking changes. Example: '1.0', '2.1'. Semantic versioning recommended.",
    )
    lead_time_days: int | None = Field(  # noqa: N815
        default=None,
        alias="leadTimeDays",
        description="Minimum lead time in calendar days. Required for policy_type='lead_times'. Example: 14 means 14 days notice required.",
    )

    # Create payroll cutoff fields
    cutoff_id: str | None = Field(  # noqa: N815
        default=None,
        alias="cutoffId",
        description="Unique cutoff identifier. Format: CUTOFF-{COUNTRY}-{SEQ} (e.g., 'CUTOFF-US-001').",
    )
    cutoff_day_of_month: int | None = Field(  # noqa: N815
        default=None,
        alias="cutoffDayOfMonth",
        ge=1,
        le=31,
        description="Day of month for payroll cutoff (1-31). New hires must be entered before this day for that month's payroll.",
    )
    processing_days: int | None = Field(  # noqa: N815
        default=None,
        alias="processingDays",
        ge=1,
        description="Number of business days required for payroll processing before cutoff. Example: 3 means data must be entered 3 business days before cutoff_day_of_month.",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Reports Meta-Tool Models
# =============================================================================


class ReportsInput(BaseModel):
    """Input model for workday_reports meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal[
        "help", "workforce_roster", "headcount", "movements", "positions", "org_hierarchy"
    ] = Field(
        ...,
        description=(
            "Action: 'help', 'workforce_roster', 'headcount', "
            "'movements', 'positions', 'org_hierarchy'"
        ),
    )

    # Common filters
    org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="orgId",
        description="Filter by organization ID",
    )
    cost_center_id: str | None = Field(  # noqa: N815
        default=None,
        alias="costCenterId",
        description="Filter by cost center ID",
    )
    as_of_date: str | None = Field(  # noqa: N815
        default=None,
        alias="asOfDate",
        description="Point-in-time date YYYY-MM-DD",
    )

    # Workforce roster specific
    employment_status: Literal["Active", "Terminated", "Leave"] | None = Field(  # noqa: N815
        default=None,
        alias="employmentStatus",
        description="Filter by employment status",
    )

    # Headcount/Movements specific
    start_date: str | None = Field(  # noqa: N815
        default=None,
        alias="startDate",
        description="Period start date YYYY-MM-DD",
    )
    end_date: str | None = Field(  # noqa: N815
        default=None,
        alias="endDate",
        description="Period end date YYYY-MM-DD",
    )
    group_by: Literal["org_id", "cost_center_id"] | None = Field(  # noqa: N815
        default=None,
        alias="groupBy",
        description="Grouping dimension for headcount",
    )
    event_type: Literal["hire", "termination", "transfer"] | None = Field(  # noqa: N815
        default=None,
        alias="eventType",
        description="Filter by event type for movements",
    )

    # Positions specific
    status: Literal["open", "filled", "closed"] | None = Field(
        default=None,
        description="Filter by position status",
    )
    job_profile_id: str | None = Field(  # noqa: N815
        default=None,
        alias="jobProfileId",
        description="Filter by job profile ID",
    )

    # Org hierarchy specific
    root_org_id: str | None = Field(  # noqa: N815
        default=None,
        alias="rootOrgId",
        description="Root org ID to start from",
    )

    # Pagination
    page_size: int | None = Field(  # noqa: N815
        default=None,
        alias="pageSize",
        ge=1,
        le=10000,
        description="Results per page (default 1000, max 10000)",
    )
    page_number: int | None = Field(  # noqa: N815
        default=None,
        alias="pageNumber",
        ge=1,
        description="Page number (default 1)",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Exceptions Meta-Tool Models
# =============================================================================


class ExceptionsInput(BaseModel):
    """Input model for workday_exceptions meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "request", "approve"] = Field(
        ..., description="Action: 'help', 'request', 'approve'"
    )

    # Request fields
    case_id: str | None = Field(  # noqa: N815
        default=None,
        alias="caseId",
        description="Case ID (required for request)",
    )
    milestone_type: (
        Literal[  # noqa: N815
            "screening", "work_authorization", "documents", "approvals"
        ]
        | None
    ) = Field(
        default=None,
        alias="milestoneType",
        description="Milestone requiring exception",
    )
    reason: str | None = Field(
        default=None,
        description="Detailed reason for exception request",
    )
    affected_policy_refs: list[str] | None = Field(  # noqa: N815
        default=None,
        alias="affectedPolicyRefs",
        description="Policies being excepted",
    )
    actor_persona: (
        Literal[
            "pre_onboarding_coordinator",
            "hr_admin",
            "hr_business_partner",
            "hiring_manager",
            "auditor",
        ]
        | None
    ) = Field(  # noqa: N815
        default=None,
        alias="actorPersona",
        description="Persona requesting the exception",
    )

    # Approve fields
    exception_id: str | None = Field(  # noqa: N815
        default=None,
        alias="exceptionId",
        description="Exception ID to approve",
    )
    approval_status: Literal["approved", "denied"] | None = Field(  # noqa: N815
        default=None,
        alias="approvalStatus",
        description="Approval decision",
    )
    approval_notes: str | None = Field(  # noqa: N815
        default=None,
        alias="approvalNotes",
        description="Mandatory notes explaining decision",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Audit Meta-Tool Models
# =============================================================================


class AuditInput(BaseModel):
    """Input model for workday_audit meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "get_history"] = Field(
        ..., description="The operation to perform. REQUIRED. Call with action='help' first."
    )

    # Get history fields
    case_id: str | None = Field(  # noqa: N815
        default=None,
        alias="caseId",
        description="Case ID (required for get_history)",
    )
    action_type: str | None = Field(  # noqa: N815
        default=None,
        alias="actionType",
        description="Filter by action type",
    )
    actor_persona: str | None = Field(  # noqa: N815
        default=None,
        alias="actorPersona",
        description="Filter by actor persona",
    )
    start_date: str | None = Field(  # noqa: N815
        default=None,
        alias="startDate",
        description="Filter from date YYYY-MM-DD",
    )
    end_date: str | None = Field(  # noqa: N815
        default=None,
        alias="endDate",
        description="Filter to date YYYY-MM-DD",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# System Meta-Tool Models
# =============================================================================


class SystemInput(BaseModel):
    """Input model for workday_system meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "health_check", "get_server_info", "schema"] = Field(
        ..., description="Action: 'help', 'health_check', 'get_server_info', 'schema'"
    )

    # Schema introspection fields
    tool_name: str | None = Field(  # noqa: N815
        default=None,
        alias="toolName",
        description="Meta-tool name to get schema for (for schema action)",
    )

    # Standard projection parameters
    include: list[str] | None = Field(None, description="Fields to include in response")
    exclude: list[str] | None = Field(None, description="Fields to exclude from response")
    key_list: list[str] | None = Field(  # noqa: N815
        None,
        alias="keyList",
        description="Explicit list of fields to return",
    )


# =============================================================================
# Help Meta-Tool Models (Workday Help module)
# =============================================================================


class HelpInput(BaseModel):
    """Input model for workday_help meta-tool (Help desk case management)."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal[
        "help",
        "create_case",
        "get_case",
        "update_status",
        "reassign_owner",
        "update_due_date",
        "search_cases",
        "add_timeline_event",
        "get_timeline_events",
        "get_timeline_snapshot",
        "add_message",
        "search_messages",
        "add_attachment",
        "list_attachments",
        "query_audit",
    ] = Field(
        ...,
        description=(
            "Action: 'help', 'create_case', 'get_case', 'update_status', "
            "'reassign_owner', 'update_due_date', 'search_cases', "
            "'add_timeline_event', 'get_timeline_events', 'get_timeline_snapshot', "
            "'add_message', 'search_messages', 'add_attachment', 'list_attachments', "
            "'query_audit'"
        ),
    )

    # Common identifiers
    case_id: str | None = Field(
        default=None,
        alias="caseId",
        description="Case ID (required for most actions)",
    )

    # Case creation fields
    case_type: Literal["Pre-Onboarding"] | None = Field(
        default=None,
        alias="caseType",
        description="Case type (required for create_case)",
    )
    owner: str | None = Field(
        default=None,
        description="Case owner email (required for create_case)",
    )
    candidate_identifier: str | None = Field(
        default=None,
        alias="candidateIdentifier",
        description="Unique candidate identifier (required for create_case)",
    )
    status: Literal["Open", "Waiting", "In Progress", "Resolved", "Closed"] | None = Field(
        default=None,
        description="Case status",
    )
    due_date: str | None = Field(
        default=None,
        alias="dueDate",
        description="Due date YYYY-MM-DD",
    )

    # Status update fields
    current_status: Literal["Open", "Waiting", "In Progress", "Resolved", "Closed"] | None = Field(
        default=None,
        alias="currentStatus",
        description="Current status for optimistic concurrency (required for update_status)",
    )
    new_status: Literal["Open", "Waiting", "In Progress", "Resolved", "Closed"] | None = Field(
        default=None,
        alias="newStatus",
        description="New status (required for update_status)",
    )
    rationale: str | None = Field(
        default=None,
        description="Rationale for status change (required for updates)",
    )

    # Owner reassignment
    new_owner: str | None = Field(
        default=None,
        alias="newOwner",
        description="New owner email (required for reassign_owner)",
    )

    # Due date update
    new_due_date: str | None = Field(
        default=None,
        alias="newDueDate",
        description="New due date YYYY-MM-DD (required for update_due_date)",
    )

    # Timeline event fields
    event_type: str | None = Field(
        default=None,
        alias="eventType",
        description="Timeline event type (required for add_timeline_event)",
    )
    notes: str | None = Field(
        default=None,
        description="Event notes",
    )
    as_of_date: str | None = Field(
        default=None,
        alias="asOfDate",
        description="Point-in-time snapshot date",
    )

    # Message fields
    direction: Literal["internal", "inbound", "outbound"] | None = Field(
        default=None,
        description="Message direction (required for add_message)",
    )
    sender: str | None = Field(
        default=None,
        description="Message sender (required for add_message)",
    )
    body: str | None = Field(
        default=None,
        description="Message body (required for add_message)",
    )
    audience: Literal["candidate", "hiring_manager", "recruiter", "internal_hr"] | None = Field(
        default=None,
        description="Target audience for message",
    )
    message_id: str | None = Field(
        default=None,
        alias="messageId",
        description="Message ID for search filter",
    )

    # Attachment fields
    filename: str | None = Field(
        default=None,
        description="Attachment filename (required for add_attachment)",
    )
    uploader: str | None = Field(
        default=None,
        description="Uploader email (required for add_attachment)",
    )
    mime_type: str | None = Field(
        default=None,
        alias="mimeType",
        description="MIME type",
    )
    size_bytes: int | None = Field(
        default=None,
        alias="sizeBytes",
        description="File size in bytes",
    )
    source: str | None = Field(
        default=None,
        description="Attachment source (e.g., 'ATS', 'Background Check Vendor')",
    )
    external_reference: str | None = Field(
        default=None,
        alias="externalReference",
        description="External reference URL",
    )

    # Audit query fields
    action_type: str | None = Field(
        default=None,
        alias="actionType",
        description="Filter by action type for audit query",
    )
    created_after: str | None = Field(
        default=None,
        alias="createdAfter",
        description="Filter from date ISO 8601",
    )
    created_before: str | None = Field(
        default=None,
        alias="createdBefore",
        description="Filter to date ISO 8601",
    )

    # Actor/persona fields
    actor: str | None = Field(
        default=None,
        description="Actor email performing the action",
    )
    actor_persona: Literal["case_owner", "hr_admin", "manager", "hr_analyst"] | None = Field(
        default=None,
        alias="actorPersona",
        description="Actor persona",
    )

    # Pagination
    cursor: str | None = Field(
        default=None,
        description="Pagination cursor from previous response for fetching next page. Opaque string.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="Maximum number of results to return. Range: 1-500. Default varies by action.",
    )

    # Metadata
    metadata: dict | None = Field(
        default=None,
        description='Additional metadata as JSON object. Key-value pairs for extensibility. Example: {"source": "ATS", "priority": "high"}.',
    )


# =============================================================================
# Schema Registry
# =============================================================================

TOOL_SCHEMAS: dict[str, dict[str, type[BaseModel]]] = {
    "workday_workers": {"input": WorkersInput, "output": MetaToolOutput},
    "workday_positions": {"input": PositionsInput, "output": MetaToolOutput},
    "workday_organizations": {"input": OrganizationsInput, "output": MetaToolOutput},
    "workday_job_profiles": {"input": JobProfilesInput, "output": MetaToolOutput},
    "workday_cases": {"input": CasesInput, "output": MetaToolOutput},
    "workday_hcm": {"input": HCMInput, "output": MetaToolOutput},
    "workday_milestones": {"input": MilestonesInput, "output": MetaToolOutput},
    "workday_tasks": {"input": TasksInput, "output": MetaToolOutput},
    "workday_policies": {"input": PoliciesInput, "output": MetaToolOutput},
    "workday_reports": {"input": ReportsInput, "output": MetaToolOutput},
    "workday_exceptions": {"input": ExceptionsInput, "output": MetaToolOutput},
    "workday_audit": {"input": AuditInput, "output": MetaToolOutput},
    "workday_system": {"input": SystemInput, "output": MetaToolOutput},
    "workday_help": {"input": HelpInput, "output": MetaToolOutput},
}
