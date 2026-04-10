"""Meta-tools for Workday HCM MCP server.

This module consolidates individual tools into domain-based meta-tools with action routing.
Meta-tools reduce token usage for LLMs while maintaining full functionality.

File structure per META_TOOL_RULES.md:
- Helper functions (_check_role, _check_scope, _make_output)
- Help definition constants (WORKERS_HELP, etc.)
- Meta-tool functions (workday_workers, etc.)
- Registration function (register_meta_tools)
"""

from typing import Any

from fastmcp import FastMCP
from mcp_auth import get_current_user
from models import (
    ApproveExceptionInput,
    AssignOwnerInput,
    AttachPolicyInput,
    CaseSnapshotInput,
    ClosePositionInput,
    ConfirmStartDateInput,
    CreateCaseInput,
    CreateCostCenterInput,
    CreateJobProfileInput,
    CreateLocationInput,
    CreatePayrollCutoffInput,
    CreatePolicyInput,
    CreatePositionInput,
    CreateSupervisoryOrgInput,
    CreateTaskInput,
    CreateWorkerInput,
    GetApplicablePoliciesInput,
    GetAuditHistoryInput,
    GetCaseInput,
    GetJobProfileInput,
    GetOrgHierarchyInput,
    GetPositionInput,
    GetSupervisoryOrgInput,
    GetWorkerInput,
    HeadcountReportInput,
    ListJobProfilesInput,
    ListMilestonesInput,
    ListPositionsInput,
    ListSupervisoryOrgsInput,
    ListWorkersInput,
    MovementReportInput,
    OrgHierarchyReportInput,
    PositionReportInput,
    ReadHCMContextInput,
    ReadPositionInput,
    RequestExceptionInput,
    SearchCasesInput,
    TerminateWorkerInput,
    TransferWorkerInput,
    UpdateCaseStatusInput,
    UpdateMilestoneInput,
    UpdateReadinessInput,
    UpdateTaskInput,
    WorkforceRosterInput,
)

# Help tool schemas
from schemas.help.attachment_schemas import AddAttachmentRequest, ListAttachmentsRequest
from schemas.help.audit_schemas import QueryAuditHistoryRequest
from schemas.help.case_schemas import (
    CreateCaseRequest,
    GetCaseRequest,
    ReassignCaseOwnerRequest,
    SearchCasesRequest,
    UpdateCaseDueDateRequest,
    UpdateCaseStatusRequest,
)
from schemas.help.message_schemas import AddMessageRequest, SearchMessagesRequest
from schemas.help.timeline_schemas import (
    AddTimelineEventRequest,
    GetTimelineEventsRequest,
    GetTimelineSnapshotRequest,
)
from schemas.meta_tools import (
    TOOL_SCHEMAS,
    AuditInput,
    CasesInput,
    ExceptionsInput,
    HCMInput,
    HelpInput,
    HelpResponse,
    JobProfilesInput,
    MetaToolOutput,
    MilestonesInput,
    OrganizationsInput,
    PoliciesInput,
    PositionsInput,
    ReportsInput,
    SystemInput,
    TasksInput,
    WorkersInput,
)

# Import individual tool functions (NOT repositories)
from tools.audit_tools import workday_audit_get_history
from tools.case_tools import (
    workday_assign_owner_case,
    workday_create_case,
    workday_get_case,
    workday_search_case,
    workday_snapshot_case,
    workday_update_case,
)
from tools.cost_center_tools import workday_create_cost_center
from tools.exception_tools import workday_exception_approve, workday_exception_request
from tools.hcm_tools import (
    workday_hcm_confirm_start_date,
    workday_hcm_read_context,
    workday_hcm_read_position,
    workday_hcm_update_readiness,
)
from tools.health_tools import workday_health_check

# Help tools (Workday Help module)
from tools.help.attachments import workday_help_attachments_add, workday_help_attachments_list
from tools.help.audit import workday_help_audit_query_history
from tools.help.cases import (
    workday_help_cases_create,
    workday_help_cases_get,
    workday_help_cases_reassign_owner,
    workday_help_cases_search,
    workday_help_cases_update_due_date,
    workday_help_cases_update_status,
)
from tools.help.messages import workday_help_messages_add, workday_help_messages_search
from tools.help.timeline import (
    workday_help_timeline_add_event,
    workday_help_timeline_get_events,
    workday_help_timeline_get_snapshot,
)
from tools.job_profile_tools import (
    workday_create_job_profile,
    workday_get_job_profile,
    workday_list_job_profiles,
)
from tools.location_tools import workday_create_location
from tools.milestone_tools import workday_milestones_list, workday_milestones_update
from tools.org_tools import (
    workday_create_org,
    workday_get_org,
    workday_get_org_hierarchy,
    workday_list_orgs,
)
from tools.policy_tools import (
    workday_policies_attach_to_case,
    workday_policies_create,
    workday_policies_create_payroll_cutoff,
    workday_policies_get_applicable,
)
from tools.position_tools import (
    workday_close_position,
    workday_create_position,
    workday_get_position,
    workday_list_positions,
)
from tools.report_tools import (
    workday_report_headcount,
    workday_report_movements,
    workday_report_org_hierarchy,
    workday_report_positions,
    workday_report_workforce_roster,
)
from tools.task_tools import workday_tasks_create, workday_tasks_update
from tools.worker_tools import (
    workday_get_worker,
    workday_hire_worker,
    workday_list_workers,
    workday_terminate_worker,
    workday_transfer_worker,
)

# =============================================================================
# Helper Functions
# =============================================================================


def _check_role(*required_roles: str) -> None:
    """Check if current user has any of the required roles. Raises PermissionError if not."""
    user = get_current_user()
    if not user:
        raise PermissionError("Authentication required")
    user_roles = set(user.get("roles", []))
    if not user_roles.intersection(required_roles):
        raise PermissionError(f"Access denied: Requires one of roles {required_roles}")


def _check_scope(required_scope: str) -> None:
    """Check if current user has required scope. Raises PermissionError if not."""
    user = get_current_user()
    if not user:
        raise PermissionError("Authentication required")
    user_scopes = set(user.get("scopes", []))
    if required_scope not in user_scopes:
        raise PermissionError(f"Access denied: Missing scope '{required_scope}'")


def _make_output(
    action: str,
    result: Any = None,
    *,
    error: str | None = None,
) -> MetaToolOutput:
    """Create MetaToolOutput, detecting error responses from underlying functions."""
    if error is not None:
        return MetaToolOutput(action=action, error=error)

    if result is None:
        return MetaToolOutput(action=action, data=None)

    if hasattr(result, "model_dump"):
        result = result.model_dump()

    if isinstance(result, dict) and "error" in result:
        error_info = result["error"]
        if isinstance(error_info, dict):
            error_msg = error_info.get("message", str(error_info))
        else:
            error_msg = str(error_info)
        return MetaToolOutput(action=action, error=error_msg)

    return MetaToolOutput(action=action, data=result)


# =============================================================================
# Workers Meta-Tool
# =============================================================================

WORKERS_HELP = HelpResponse(
    tool_name="workday_workers",
    description="Manage worker lifecycle in Workday HCM (hire, get, list, transfer, terminate)",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "hire": {
            "description": "Hire a new worker in Workday HCM",
            "required_params": [
                "worker_id (str, e.g., 'WRK-00123')",
                "job_profile_id (str, e.g., 'JP-ENG-002')",
                "org_id (str, e.g., 'ORG-001')",
                "cost_center_id (str, e.g., 'CC-001')",
                "hire_date (str, YYYY-MM-DD, e.g., '2024-03-15')",
            ],
            "optional_params": [
                "location_id (str)",
                "position_id (str, e.g., 'POS-00456')",
                "fte (float, 0.0-1.0, default 1.0)",
                "effective_date (str, YYYY-MM-DD)",
            ],
        },
        "get": {
            "description": "Retrieve detailed information about a worker by ID",
            "required_params": ["worker_id (str, e.g., 'WRK-00123')"],
            "optional_params": ["as_of_date (str, YYYY-MM-DD)"],
        },
        "list": {
            "description": "List workers with pagination and filtering",
            "required_params": [],
            "optional_params": [
                "page_size (int, 1-1000, default 100)",
                "page_number (int, default 1)",
                "org_id (str)",
                "cost_center_id (str)",
                "employment_status (str: Active|Terminated|Leave)",
                "as_of_date (str, YYYY-MM-DD)",
            ],
        },
        "transfer": {
            "description": "Transfer a worker to a new org, cost center, job profile, or position. At least one 'new_*' field required.",
            "required_params": [
                "worker_id (str, e.g., 'WRK-00123')",
                "transfer_date (str, YYYY-MM-DD)",
            ],
            "optional_params": [
                "new_org_id (str)",
                "new_cost_center_id (str)",
                "new_job_profile_id (str)",
                "new_position_id (str)",
                "new_fte (float, 0.0-1.0)",
                "effective_date (str, YYYY-MM-DD)",
            ],
        },
        "terminate": {
            "description": "Terminate a worker's employment (permanent) or place on leave. Worker must have 'Active' status.",
            "required_params": [
                "worker_id (str, e.g., 'WRK-00123')",
                "termination_date (str, YYYY-MM-DD)",
            ],
            "optional_params": ["effective_date (str, YYYY-MM-DD)"],
        },
    },
)


async def workday_workers(request: WorkersInput) -> MetaToolOutput:
    """Manage worker lifecycle in Workday HCM."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=WORKERS_HELP)
        case "hire":
            if request.worker_id is None:
                return _make_output("hire", error="worker_id required")
            if request.job_profile_id is None:
                return _make_output("hire", error="job_profile_id required")
            if request.org_id is None:
                return _make_output("hire", error="org_id required")
            if request.cost_center_id is None:
                return _make_output("hire", error="cost_center_id required")
            if request.hire_date is None:
                return _make_output("hire", error="hire_date required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = CreateWorkerInput(
                worker_id=request.worker_id,
                job_profile_id=request.job_profile_id,
                org_id=request.org_id,
                cost_center_id=request.cost_center_id,
                location_id=request.location_id,
                position_id=request.position_id,
                fte=request.fte if request.fte is not None else 1.0,
                hire_date=request.hire_date,
                effective_date=request.effective_date,
            )
            result = await workday_hire_worker(tool_input)
            return _make_output("hire", result)
        case "get":
            if request.worker_id is None:
                return _make_output("get", error="worker_id required")
            _check_scope("read")
            tool_input = GetWorkerInput(worker_id=request.worker_id, as_of_date=request.as_of_date)
            result = await workday_get_worker(tool_input)
            return _make_output("get", result)
        case "list":
            _check_scope("read")
            tool_input = ListWorkersInput(
                page_size=request.page_size if request.page_size is not None else 100,
                page_number=request.page_number if request.page_number is not None else 1,
                org_id=request.org_id,
                cost_center_id=request.cost_center_id,
                employment_status=request.employment_status,
                as_of_date=request.as_of_date,
            )
            result = await workday_list_workers(tool_input)
            return _make_output("list", result)
        case "transfer":
            if request.worker_id is None:
                return _make_output("transfer", error="worker_id required")
            if request.transfer_date is None:
                return _make_output("transfer", error="transfer_date required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = TransferWorkerInput(
                worker_id=request.worker_id,
                new_org_id=request.new_org_id,
                new_cost_center_id=request.new_cost_center_id,
                new_job_profile_id=request.new_job_profile_id,
                new_position_id=request.new_position_id,
                new_fte=request.new_fte,
                transfer_date=request.transfer_date,
                effective_date=request.effective_date,
            )
            result = await workday_transfer_worker(tool_input)
            return _make_output("transfer", result)
        case "terminate":
            if request.worker_id is None:
                return _make_output("terminate", error="worker_id required")
            if request.termination_date is None:
                return _make_output("terminate", error="termination_date required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = TerminateWorkerInput(
                worker_id=request.worker_id,
                termination_date=request.termination_date,
                effective_date=request.effective_date,
            )
            result = await workday_terminate_worker(tool_input)
            return _make_output("terminate", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Positions Meta-Tool
# =============================================================================

POSITIONS_HELP = HelpResponse(
    tool_name="workday_positions",
    description="Manage positions in Workday HCM (create, get, list, close)",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new position in Workday HCM",
            "required_params": ["position_id", "job_profile_id", "org_id"],
            "optional_params": ["fte"],
        },
        "get": {
            "description": "Retrieve detailed information about a position by ID",
            "required_params": ["position_id"],
            "optional_params": [],
        },
        "list": {
            "description": "List positions with pagination and filtering",
            "required_params": [],
            "optional_params": ["page_size", "page_number", "org_id", "status", "job_profile_id"],
        },
        "close": {
            "description": "Close a position, marking it as unavailable for hiring",
            "required_params": ["position_id"],
            "optional_params": [],
        },
    },
)


async def workday_positions(request: PositionsInput) -> MetaToolOutput:
    """Manage positions in Workday HCM."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=POSITIONS_HELP)
        case "create":
            if request.position_id is None:
                return _make_output("create", error="position_id required")
            if request.job_profile_id is None:
                return _make_output("create", error="job_profile_id required")
            if request.org_id is None:
                return _make_output("create", error="org_id required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = CreatePositionInput(
                position_id=request.position_id,
                job_profile_id=request.job_profile_id,
                org_id=request.org_id,
                fte=request.fte if request.fte is not None else 1.0,
            )
            result = await workday_create_position(tool_input)
            return _make_output("create", result)
        case "get":
            if request.position_id is None:
                return _make_output("get", error="position_id required")
            _check_scope("read")
            tool_input = GetPositionInput(position_id=request.position_id)
            result = await workday_get_position(tool_input)
            return _make_output("get", result)
        case "list":
            _check_scope("read")
            tool_input = ListPositionsInput(
                page_size=request.page_size if request.page_size is not None else 100,
                page_number=request.page_number if request.page_number is not None else 1,
                org_id=request.org_id,
                status=request.status,
                job_profile_id=request.job_profile_id,
            )
            result = await workday_list_positions(tool_input)
            return _make_output("list", result)
        case "close":
            if request.position_id is None:
                return _make_output("close", error="position_id required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = ClosePositionInput(position_id=request.position_id)
            result = await workday_close_position(tool_input)
            return _make_output("close", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Organizations Meta-Tool
# =============================================================================

ORGANIZATIONS_HELP = HelpResponse(
    tool_name="workday_organizations",
    description="Manage organizations, cost centers, and locations in Workday HCM",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new supervisory organization",
            "required_params": ["org_id", "org_name"],
            "optional_params": ["org_type", "parent_org_id", "manager_worker_id"],
        },
        "get": {
            "description": "Retrieve detailed information about an organization by ID",
            "required_params": ["org_id"],
            "optional_params": [],
        },
        "list": {
            "description": "List organizations with pagination and filtering",
            "required_params": [],
            "optional_params": [
                "page_size",
                "page_number",
                "parent_org_id",
                "org_type",
                "root_only",
            ],
        },
        "hierarchy": {
            "description": "Retrieve organization hierarchy as nested tree structure",
            "required_params": [],
            "optional_params": ["root_org_id"],
        },
        "create_cost_center": {
            "description": "Create a new cost center",
            "required_params": ["cost_center_id", "cost_center_name", "org_id"],
            "optional_params": [],
        },
        "create_location": {
            "description": "Create a new location",
            "required_params": ["location_id", "location_name", "country"],
            "optional_params": ["city"],
        },
    },
)


async def workday_organizations(request: OrganizationsInput) -> MetaToolOutput:
    """Manage organizations, cost centers, and locations in Workday HCM."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=ORGANIZATIONS_HELP)
        case "create":
            if request.org_id is None:
                return _make_output("create", error="org_id required")
            if request.org_name is None:
                return _make_output("create", error="org_name required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = CreateSupervisoryOrgInput(
                org_id=request.org_id,
                org_name=request.org_name,
                org_type=request.org_type if request.org_type is not None else "Supervisory",
                parent_org_id=request.parent_org_id,
                manager_worker_id=request.manager_worker_id,
            )
            result = await workday_create_org(tool_input)
            return _make_output("create", result)
        case "get":
            if request.org_id is None:
                return _make_output("get", error="org_id required")
            _check_scope("read")
            tool_input = GetSupervisoryOrgInput(org_id=request.org_id)
            result = await workday_get_org(tool_input)
            return _make_output("get", result)
        case "list":
            _check_scope("read")
            tool_input = ListSupervisoryOrgsInput(
                page_size=request.page_size if request.page_size is not None else 100,
                page_number=request.page_number if request.page_number is not None else 1,
                parent_org_id=request.parent_org_id,
                org_type=request.org_type,
                root_only=request.root_only if request.root_only is not None else False,
            )
            result = await workday_list_orgs(tool_input)
            return _make_output("list", result)
        case "hierarchy":
            _check_scope("read")
            tool_input = GetOrgHierarchyInput(root_org_id=request.root_org_id)
            result = await workday_get_org_hierarchy(tool_input)
            return _make_output("hierarchy", result)
        case "create_cost_center":
            if request.cost_center_id is None:
                return _make_output("create_cost_center", error="cost_center_id required")
            if request.cost_center_name is None:
                return _make_output("create_cost_center", error="cost_center_name required")
            if request.org_id is None:
                return _make_output("create_cost_center", error="org_id required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = CreateCostCenterInput(
                cost_center_id=request.cost_center_id,
                cost_center_name=request.cost_center_name,
                org_id=request.org_id,
            )
            result = await workday_create_cost_center(tool_input)
            return _make_output("create_cost_center", result)
        case "create_location":
            if request.location_id is None:
                return _make_output("create_location", error="location_id required")
            if request.location_name is None:
                return _make_output("create_location", error="location_name required")
            if request.country is None:
                return _make_output("create_location", error="country required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = CreateLocationInput(
                location_id=request.location_id,
                location_name=request.location_name,
                city=request.city,
                country=request.country,
            )
            result = await workday_create_location(tool_input)
            return _make_output("create_location", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Job Profiles Meta-Tool
# =============================================================================

JOB_PROFILES_HELP = HelpResponse(
    tool_name="workday_job_profiles",
    description="Manage job profiles in Workday HCM (create, get, list)",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new job profile",
            "required_params": ["job_profile_id", "title", "job_family"],
            "optional_params": ["job_level"],
        },
        "get": {
            "description": "Retrieve detailed information about a job profile by ID",
            "required_params": ["job_profile_id"],
            "optional_params": [],
        },
        "list": {
            "description": "List job profiles with pagination and filtering",
            "required_params": [],
            "optional_params": ["page_size", "page_number", "job_family"],
        },
    },
)


async def workday_job_profiles(request: JobProfilesInput) -> MetaToolOutput:
    """Manage job profiles in Workday HCM."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=JOB_PROFILES_HELP)
        case "create":
            if request.job_profile_id is None:
                return _make_output("create", error="job_profile_id required")
            if request.title is None:
                return _make_output("create", error="title required")
            if request.job_family is None:
                return _make_output("create", error="job_family required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = CreateJobProfileInput(
                job_profile_id=request.job_profile_id,
                title=request.title,
                job_family=request.job_family,
                job_level=request.job_level,
            )
            result = await workday_create_job_profile(tool_input)
            return _make_output("create", result)
        case "get":
            if request.job_profile_id is None:
                return _make_output("get", error="job_profile_id required")
            _check_scope("read")
            tool_input = GetJobProfileInput(job_profile_id=request.job_profile_id)
            result = await workday_get_job_profile(tool_input)
            return _make_output("get", result)
        case "list":
            _check_scope("read")
            tool_input = ListJobProfilesInput(
                page_size=request.page_size if request.page_size is not None else 100,
                page_number=request.page_number if request.page_number is not None else 1,
                job_family=request.job_family,
            )
            result = await workday_list_job_profiles(tool_input)
            return _make_output("list", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Cases Meta-Tool
# =============================================================================

CASES_HELP = HelpResponse(
    tool_name="workday_cases",
    description="Manage pre-onboarding cases in Workday HCM",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new pre-onboarding case",
            "required_params": [
                "case_id (str, e.g., 'CASE-001')",
                "candidate_id (str, e.g., 'CAND-12345')",
                "role (str, e.g., 'Software Engineer')",
                "country (str, ISO 3166-1 alpha-2, e.g., 'US', 'GB')",
            ],
            "optional_params": [
                "requisition_id (str)",
                "employment_type (str: full_time|part_time|contractor, default full_time)",
                "owner_persona (str: pre_onboarding_coordinator|hr_admin|hr_business_partner|hiring_manager|auditor)",
                "proposed_start_date (str, YYYY-MM-DD)",
                "due_date (str, YYYY-MM-DD)",
                "notes (str)",
            ],
        },
        "get": {
            "description": "Retrieve detailed information about a case by ID",
            "required_params": ["case_id (str, e.g., 'CASE-001')"],
            "optional_params": [
                "include_tasks (bool, default true)",
                "include_audit (bool, default false)",
            ],
        },
        "update": {
            "description": "Update case status. Valid transitions: open->in_progress, in_progress->pending_approval|resolved, resolved->closed",
            "required_params": [
                "case_id (str)",
                "new_status (str: open|in_progress|pending_approval|resolved|closed)",
                "rationale (str)",
                "actor_persona (str: pre_onboarding_coordinator|hr_admin|hr_business_partner|hiring_manager|auditor)",
            ],
            "optional_params": [],
        },
        "assign_owner": {
            "description": "Assign a new owner to a case",
            "required_params": [
                "case_id (str)",
                "new_owner_persona (str: pre_onboarding_coordinator|hr_admin|hr_business_partner|hiring_manager|auditor)",
                "rationale (str)",
                "actor_persona (str)",
            ],
            "optional_params": [],
        },
        "search": {
            "description": "Search cases with filters",
            "required_params": [],
            "optional_params": [
                "status (str: open|in_progress|pending_approval|resolved|closed)",
                "owner_persona (str)",
                "country (str, ISO 3166-1 alpha-2)",
                "role (str)",
                "due_date_before (str, YYYY-MM-DD)",
                "due_date_after (str, YYYY-MM-DD)",
                "page_size (int, 1-500, default 50)",
                "page_number (int, default 1)",
            ],
        },
        "snapshot": {
            "description": "Get a complete snapshot of a case",
            "required_params": ["case_id (str)"],
            "optional_params": ["as_of_date (str, YYYY-MM-DD)"],
        },
    },
)


async def workday_cases(request: CasesInput) -> MetaToolOutput:
    """Manage pre-onboarding cases in Workday HCM."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=CASES_HELP)
        case "create":
            if request.case_id is None:
                return _make_output("create", error="case_id required")
            if request.candidate_id is None:
                return _make_output("create", error="candidate_id required")
            if request.role is None:
                return _make_output("create", error="role required")
            if request.country is None:
                return _make_output("create", error="country required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            emp_type = request.employment_type or "full_time"
            owner = request.owner_persona or "pre_onboarding_coordinator"
            tool_input = CreateCaseInput(
                case_id=request.case_id,
                candidate_id=request.candidate_id,
                requisition_id=request.requisition_id,
                role=request.role,
                country=request.country,
                employment_type=emp_type,
                owner_persona=owner,
                proposed_start_date=request.proposed_start_date,
                due_date=request.due_date,
                notes=request.notes,
            )
            result = await workday_create_case(tool_input)
            return _make_output("create", result)
        case "get":
            if request.case_id is None:
                return _make_output("get", error="case_id required")
            _check_scope("read")
            tool_input = GetCaseInput(
                case_id=request.case_id,
                include_tasks=request.include_tasks if request.include_tasks is not None else True,
                include_audit=request.include_audit if request.include_audit is not None else False,
            )
            result = await workday_get_case(tool_input)
            return _make_output("get", result)
        case "update":
            if request.case_id is None:
                return _make_output("update", error="case_id required")
            if request.new_status is None:
                return _make_output("update", error="new_status required")
            if request.rationale is None:
                return _make_output("update", error="rationale required")
            if request.actor_persona is None:
                return _make_output("update", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin", "hr_business_partner")
            tool_input = UpdateCaseStatusInput(
                case_id=request.case_id,
                new_status=request.new_status,
                rationale=request.rationale,
                actor_persona=request.actor_persona,
            )
            result = await workday_update_case(tool_input)
            return _make_output("update", result)
        case "assign_owner":
            if request.case_id is None:
                return _make_output("assign_owner", error="case_id required")
            if request.new_owner_persona is None:
                return _make_output("assign_owner", error="new_owner_persona required")
            if request.rationale is None:
                return _make_output("assign_owner", error="rationale required")
            if request.actor_persona is None:
                return _make_output("assign_owner", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = AssignOwnerInput(
                case_id=request.case_id,
                new_owner_persona=request.new_owner_persona,
                rationale=request.rationale,
                actor_persona=request.actor_persona,
            )
            result = await workday_assign_owner_case(tool_input)
            return _make_output("assign_owner", result)
        case "search":
            _check_scope("read")
            tool_input = SearchCasesInput(
                status=request.status,
                owner_persona=request.owner_persona,
                country=request.country,
                role=request.role,
                due_date_before=request.due_date_before,
                due_date_after=request.due_date_after,
                page_size=request.page_size if request.page_size is not None else 50,
                page_number=request.page_number if request.page_number is not None else 1,
            )
            result = await workday_search_case(tool_input)
            return _make_output("search", result)
        case "snapshot":
            if request.case_id is None:
                return _make_output("snapshot", error="case_id required")
            _check_scope("read")
            tool_input = CaseSnapshotInput(case_id=request.case_id, as_of_date=request.as_of_date)
            result = await workday_snapshot_case(tool_input)
            return _make_output("snapshot", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# HCM Meta-Tool
# =============================================================================

HCM_HELP = HelpResponse(
    tool_name="workday_hcm",
    description="Read and write HCM context for pre-onboarding cases",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "read_context": {
            "description": "Read HCM context for a case",
            "required_params": ["case_id (str, e.g., 'CASE-001')"],
            "optional_params": [],
        },
        "read_position": {
            "description": "Read position context with derived policy requirements",
            "required_params": ["case_id (str)"],
            "optional_params": [],
        },
        "confirm_start_date": {
            "description": "Confirm start date (gated write-back). Requires all milestones completed, lead time and payroll constraints satisfied.",
            "required_params": [
                "case_id (str)",
                "confirmed_start_date (str, YYYY-MM-DD)",
                "policy_refs (list[str], e.g., ['POLICY-US-001'])",
                "evidence_links (list[str], URLs/references)",
                "rationale (str)",
                "actor_persona (str: pre_onboarding_coordinator|hr_admin|hr_business_partner|hiring_manager|auditor)",
            ],
            "optional_params": [],
        },
        "update_readiness": {
            "description": "Update onboarding readiness flag. Setting to true triggers HCM write-back.",
            "required_params": [
                "case_id (str)",
                "onboarding_readiness (bool)",
                "policy_refs (list[str])",
                "evidence_links (list[str])",
                "rationale (str)",
                "actor_persona (str)",
            ],
            "optional_params": [],
        },
    },
)


async def workday_hcm(request: HCMInput) -> MetaToolOutput:
    """Read and write HCM context for pre-onboarding cases."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=HCM_HELP)
        case "read_context":
            if request.case_id is None:
                return _make_output("read_context", error="case_id required")
            _check_scope("read")
            tool_input = ReadHCMContextInput(case_id=request.case_id)
            result = await workday_hcm_read_context(tool_input)
            return _make_output("read_context", result)
        case "read_position":
            if request.case_id is None:
                return _make_output("read_position", error="case_id required")
            _check_scope("read")
            tool_input = ReadPositionInput(case_id=request.case_id)
            result = await workday_hcm_read_position(tool_input)
            return _make_output("read_position", result)
        case "confirm_start_date":
            if request.case_id is None:
                return _make_output("confirm_start_date", error="case_id required")
            if request.confirmed_start_date is None:
                return _make_output("confirm_start_date", error="confirmed_start_date required")
            if request.policy_refs is None:
                return _make_output("confirm_start_date", error="policy_refs required")
            if request.evidence_links is None:
                return _make_output("confirm_start_date", error="evidence_links required")
            if request.rationale is None:
                return _make_output("confirm_start_date", error="rationale required")
            if request.actor_persona is None:
                return _make_output("confirm_start_date", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = ConfirmStartDateInput(
                case_id=request.case_id,
                confirmed_start_date=request.confirmed_start_date,
                policy_refs=request.policy_refs,
                evidence_links=request.evidence_links,
                rationale=request.rationale,
                actor_persona=request.actor_persona,
            )
            result = await workday_hcm_confirm_start_date(tool_input)
            return _make_output("confirm_start_date", result)
        case "update_readiness":
            if request.case_id is None:
                return _make_output("update_readiness", error="case_id required")
            if request.onboarding_readiness is None:
                return _make_output("update_readiness", error="onboarding_readiness required")
            if request.policy_refs is None:
                return _make_output("update_readiness", error="policy_refs required")
            if request.evidence_links is None:
                return _make_output("update_readiness", error="evidence_links required")
            if request.rationale is None:
                return _make_output("update_readiness", error="rationale required")
            if request.actor_persona is None:
                return _make_output("update_readiness", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = UpdateReadinessInput(
                case_id=request.case_id,
                onboarding_readiness=request.onboarding_readiness,
                policy_refs=request.policy_refs,
                evidence_links=request.evidence_links,
                rationale=request.rationale,
                actor_persona=request.actor_persona,
            )
            result = await workday_hcm_update_readiness(tool_input)
            return _make_output("update_readiness", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Milestones Meta-Tool
# =============================================================================

MILESTONES_HELP = HelpResponse(
    tool_name="workday_milestones",
    description="Manage milestones for pre-onboarding cases",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "list": {
            "description": "List milestones for a case",
            "required_params": ["case_id (str, e.g., 'CASE-001')"],
            "optional_params": [],
        },
        "update": {
            "description": "Update a milestone status. Valid transitions: pending->in_progress|waived|blocked, in_progress->completed|blocked, blocked->in_progress|waived",
            "required_params": [
                "case_id (str)",
                "milestone_type (str: screening|work_authorization|documents|approvals)",
                "new_status (str: pending|in_progress|completed|waived|blocked)",
                "actor_persona (str: pre_onboarding_coordinator|hr_admin|hr_business_partner|hiring_manager|auditor)",
            ],
            "optional_params": [
                "evidence_link (str, URL - required when status='completed')",
                "notes (str)",
            ],
        },
    },
)


async def workday_milestones(request: MilestonesInput) -> MetaToolOutput:
    """Manage milestones for pre-onboarding cases."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=MILESTONES_HELP)
        case "list":
            if request.case_id is None:
                return _make_output("list", error="case_id required")
            _check_scope("read")
            tool_input = ListMilestonesInput(case_id=request.case_id)
            result = await workday_milestones_list(tool_input)
            return _make_output("list", result)
        case "update":
            if request.case_id is None:
                return _make_output("update", error="case_id required")
            if request.milestone_type is None:
                return _make_output("update", error="milestone_type required")
            if request.new_status is None:
                return _make_output("update", error="new_status required")
            if request.actor_persona is None:
                return _make_output("update", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin", "hr_business_partner")
            tool_input = UpdateMilestoneInput(
                case_id=request.case_id,
                milestone_type=request.milestone_type,
                new_status=request.new_status,
                evidence_link=request.evidence_link,
                notes=request.notes,
                actor_persona=request.actor_persona,
            )
            result = await workday_milestones_update(tool_input)
            return _make_output("update", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Tasks Meta-Tool
# =============================================================================

TASKS_HELP = HelpResponse(
    tool_name="workday_tasks",
    description="Manage tasks for pre-onboarding cases",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new task for a case",
            "required_params": ["case_id", "title", "owner_persona"],
            "optional_params": ["milestone_type", "due_date", "notes"],
        },
        "update": {
            "description": "Update a task",
            "required_params": ["task_id", "actor_persona"],
            "optional_params": ["new_status", "new_owner_persona", "notes"],
        },
    },
)


async def workday_tasks(request: TasksInput) -> MetaToolOutput:
    """Manage tasks for pre-onboarding cases."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=TASKS_HELP)
        case "create":
            if request.case_id is None:
                return _make_output("create", error="case_id required")
            if request.title is None:
                return _make_output("create", error="title required")
            if request.owner_persona is None:
                return _make_output("create", error="owner_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = CreateTaskInput(
                case_id=request.case_id,
                milestone_type=request.milestone_type,
                title=request.title,
                owner_persona=request.owner_persona,
                due_date=request.due_date,
                notes=request.notes,
            )
            result = await workday_tasks_create(tool_input)
            return _make_output("create", result)
        case "update":
            if request.task_id is None:
                return _make_output("update", error="task_id required")
            if request.actor_persona is None:
                return _make_output("update", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin", "hr_business_partner")
            tool_input = UpdateTaskInput(
                task_id=request.task_id,
                new_status=request.new_status,
                new_owner_persona=request.new_owner_persona,
                notes=request.notes,
                actor_persona=request.actor_persona,
            )
            result = await workday_tasks_update(tool_input)
            return _make_output("update", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Policies Meta-Tool
# =============================================================================

POLICIES_HELP = HelpResponse(
    tool_name="workday_policies",
    description="Manage policies and payroll cutoffs for pre-onboarding",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "get_applicable": {
            "description": "Get applicable policies for a country/role/employment type",
            "required_params": ["country (str, ISO 3166-1 alpha-2, e.g., 'US', 'GB')"],
            "optional_params": [
                "role (str)",
                "employment_type (str: full_time|part_time|contractor)",
                "policy_type (str: prerequisites|lead_times|payroll_cutoffs|constraints)",
                "as_of_date (str, YYYY-MM-DD)",
            ],
        },
        "attach_to_case": {
            "description": "Attach policies to a case",
            "required_params": [
                "case_id (str)",
                "policy_ids (list[str], e.g., ['POLICY-US-001'])",
                "decision_context (str)",
                "actor_persona (str)",
            ],
            "optional_params": [],
        },
        "create": {
            "description": "Create a new policy",
            "required_params": [
                "policy_id (str, e.g., 'POLICY-US-LEAD-TIME-001')",
                "country (str, ISO 3166-1 alpha-2)",
                "policy_type (str: prerequisites|lead_times|payroll_cutoffs|constraints)",
                "content (dict, JSON object with policy details)",
                "effective_date (str, YYYY-MM-DD)",
                "version (str, e.g., '1.0')",
            ],
            "optional_params": [
                "role (str)",
                "employment_type (str)",
                "lead_time_days (int, required for lead_times type)",
            ],
        },
        "create_payroll_cutoff": {
            "description": "Create a payroll cutoff rule",
            "required_params": [
                "cutoff_id (str, e.g., 'CUTOFF-US-001')",
                "country (str, ISO 3166-1 alpha-2)",
                "cutoff_day_of_month (int, 1-31)",
                "processing_days (int, business days before cutoff)",
                "effective_date (str, YYYY-MM-DD)",
            ],
            "optional_params": [],
        },
    },
)


async def workday_policies(request: PoliciesInput) -> MetaToolOutput:
    """Manage policies and payroll cutoffs for pre-onboarding."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=POLICIES_HELP)
        case "get_applicable":
            if request.country is None:
                return _make_output("get_applicable", error="country required")
            _check_scope("read")
            tool_input = GetApplicablePoliciesInput(
                country=request.country,
                role=request.role,
                employment_type=request.employment_type,
                policy_type=request.policy_type,
                as_of_date=request.as_of_date,
            )
            result = await workday_policies_get_applicable(tool_input)
            return _make_output("get_applicable", result)
        case "attach_to_case":
            if request.case_id is None:
                return _make_output("attach_to_case", error="case_id required")
            if request.policy_ids is None:
                return _make_output("attach_to_case", error="policy_ids required")
            if request.decision_context is None:
                return _make_output("attach_to_case", error="decision_context required")
            if request.actor_persona is None:
                return _make_output("attach_to_case", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin")
            tool_input = AttachPolicyInput(
                case_id=request.case_id,
                policy_ids=request.policy_ids,
                decision_context=request.decision_context,
                actor_persona=request.actor_persona,
            )
            result = await workday_policies_attach_to_case(tool_input)
            return _make_output("attach_to_case", result)
        case "create":
            if request.policy_id is None:
                return _make_output("create", error="policy_id required")
            if request.country is None:
                return _make_output("create", error="country required")
            if request.policy_type is None:
                return _make_output("create", error="policy_type required")
            if request.content is None:
                return _make_output("create", error="content required")
            if request.effective_date is None:
                return _make_output("create", error="effective_date required")
            if request.version is None:
                return _make_output("create", error="version required")
            _check_role("hr_admin")
            tool_input = CreatePolicyInput(
                policy_id=request.policy_id,
                country=request.country,
                policy_type=request.policy_type,
                content=request.content,
                effective_date=request.effective_date,
                version=request.version,
                role=request.role,
                employment_type=request.employment_type,
                lead_time_days=request.lead_time_days,
            )
            result = await workday_policies_create(tool_input)
            return _make_output("create", result)
        case "create_payroll_cutoff":
            if request.cutoff_id is None:
                return _make_output("create_payroll_cutoff", error="cutoff_id required")
            if request.country is None:
                return _make_output("create_payroll_cutoff", error="country required")
            if request.cutoff_day_of_month is None:
                return _make_output("create_payroll_cutoff", error="cutoff_day_of_month required")
            if request.processing_days is None:
                return _make_output("create_payroll_cutoff", error="processing_days required")
            if request.effective_date is None:
                return _make_output("create_payroll_cutoff", error="effective_date required")
            _check_role("hr_admin")
            tool_input = CreatePayrollCutoffInput(
                cutoff_id=request.cutoff_id,
                country=request.country,
                cutoff_day_of_month=request.cutoff_day_of_month,
                processing_days=request.processing_days,
                effective_date=request.effective_date,
            )
            result = await workday_policies_create_payroll_cutoff(tool_input)
            return _make_output("create_payroll_cutoff", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Reports Meta-Tool
# =============================================================================

REPORTS_HELP = HelpResponse(
    tool_name="workday_reports",
    description="Generate various reports from Workday HCM data",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "workforce_roster": {
            "description": "Generate workforce roster report",
            "required_params": [],
            "optional_params": [
                "org_id",
                "cost_center_id",
                "employment_status",
                "as_of_date",
                "page_size",
                "page_number",
            ],
        },
        "headcount": {
            "description": "Generate headcount reconciliation report",
            "required_params": ["start_date", "end_date"],
            "optional_params": ["group_by", "org_id"],
        },
        "movements": {
            "description": "Generate movement/events report",
            "required_params": ["start_date", "end_date"],
            "optional_params": ["event_type", "org_id", "page_size", "page_number"],
        },
        "positions": {
            "description": "Generate position vacancy report",
            "required_params": [],
            "optional_params": ["org_id", "status", "job_profile_id", "page_size", "page_number"],
        },
        "org_hierarchy": {
            "description": "Generate organization hierarchy report",
            "required_params": [],
            "optional_params": ["root_org_id"],
        },
    },
)


async def workday_reports(request: ReportsInput) -> MetaToolOutput:
    """Generate various reports from Workday HCM data."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=REPORTS_HELP)
        case "workforce_roster":
            _check_scope("read")
            tool_input = WorkforceRosterInput(
                org_id=request.org_id,
                cost_center_id=request.cost_center_id,
                employment_status=request.employment_status,
                as_of_date=request.as_of_date,
                page_size=request.page_size if request.page_size is not None else 1000,
                page_number=request.page_number if request.page_number is not None else 1,
            )
            result = await workday_report_workforce_roster(tool_input)
            return _make_output("workforce_roster", result)
        case "headcount":
            if request.start_date is None:
                return _make_output("headcount", error="start_date required")
            if request.end_date is None:
                return _make_output("headcount", error="end_date required")
            _check_scope("read")
            tool_input = HeadcountReportInput(
                start_date=request.start_date,
                end_date=request.end_date,
                group_by=request.group_by if request.group_by is not None else "org_id",
                org_id=request.org_id,
            )
            result = await workday_report_headcount(tool_input)
            return _make_output("headcount", result)
        case "movements":
            if request.start_date is None:
                return _make_output("movements", error="start_date required")
            if request.end_date is None:
                return _make_output("movements", error="end_date required")
            _check_scope("read")
            tool_input = MovementReportInput(
                start_date=request.start_date,
                end_date=request.end_date,
                event_type=request.event_type,
                org_id=request.org_id,
                page_size=request.page_size if request.page_size is not None else 1000,
                page_number=request.page_number if request.page_number is not None else 1,
            )
            result = await workday_report_movements(tool_input)
            return _make_output("movements", result)
        case "positions":
            _check_scope("read")
            tool_input = PositionReportInput(
                org_id=request.org_id,
                status=request.status,
                job_profile_id=request.job_profile_id,
                page_size=request.page_size if request.page_size is not None else 1000,
                page_number=request.page_number if request.page_number is not None else 1,
            )
            result = await workday_report_positions(tool_input)
            return _make_output("positions", result)
        case "org_hierarchy":
            _check_scope("read")
            tool_input = OrgHierarchyReportInput(root_org_id=request.root_org_id)
            result = await workday_report_org_hierarchy(tool_input)
            return _make_output("org_hierarchy", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Exceptions Meta-Tool
# =============================================================================

EXCEPTIONS_HELP = HelpResponse(
    tool_name="workday_exceptions",
    description="Request and approve exceptions for pre-onboarding cases",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "request": {
            "description": "Request an exception for a milestone",
            "required_params": ["case_id", "milestone_type", "reason", "actor_persona"],
            "optional_params": ["affected_policy_refs"],
        },
        "approve": {
            "description": "Approve or deny an exception request",
            "required_params": ["exception_id", "approval_status", "approval_notes"],
            "optional_params": [],
        },
    },
)


async def workday_exceptions(request: ExceptionsInput) -> MetaToolOutput:
    """Request and approve exceptions for pre-onboarding cases."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=EXCEPTIONS_HELP)
        case "request":
            if request.case_id is None:
                return _make_output("request", error="case_id required")
            if request.milestone_type is None:
                return _make_output("request", error="milestone_type required")
            if request.reason is None:
                return _make_output("request", error="reason required")
            if request.actor_persona is None:
                return _make_output("request", error="actor_persona required")
            _check_role("pre_onboarding_coordinator", "hr_admin", "hr_business_partner")
            tool_input = RequestExceptionInput(
                case_id=request.case_id,
                milestone_type=request.milestone_type,
                reason=request.reason,
                affected_policy_refs=request.affected_policy_refs or [],
                actor_persona=request.actor_persona,
            )
            result = await workday_exception_request(tool_input)
            return _make_output("request", result)
        case "approve":
            if request.exception_id is None:
                return _make_output("approve", error="exception_id required")
            if request.approval_status is None:
                return _make_output("approve", error="approval_status required")
            if request.approval_notes is None:
                return _make_output("approve", error="approval_notes required")
            _check_role("hr_admin")
            tool_input = ApproveExceptionInput(
                exception_id=request.exception_id,
                approval_status=request.approval_status,
                approval_notes=request.approval_notes,
                actor_persona="hr_admin",
            )
            result = await workday_exception_approve(tool_input)
            return _make_output("approve", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Audit Meta-Tool
# =============================================================================

AUDIT_HELP = HelpResponse(
    tool_name="workday_audit",
    description="Retrieve audit history for pre-onboarding cases",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "get_history": {
            "description": "Get audit history for a case",
            "required_params": ["case_id"],
            "optional_params": ["action_type", "actor_persona", "start_date", "end_date"],
        },
    },
)


async def workday_audit(request: AuditInput) -> MetaToolOutput:
    """Retrieve audit history for pre-onboarding cases."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=AUDIT_HELP)
        case "get_history":
            if request.case_id is None:
                return _make_output("get_history", error="case_id required")
            _check_role("auditor", "hr_admin")
            tool_input = GetAuditHistoryInput(
                case_id=request.case_id,
                action_type=request.action_type,
                actor_persona=request.actor_persona,
                start_date=request.start_date,
                end_date=request.end_date,
            )
            result = await workday_audit_get_history(tool_input)
            return _make_output("get_history", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# System Meta-Tool
# =============================================================================

SYSTEM_HELP = HelpResponse(
    tool_name="workday_system",
    description="System utilities: health check, server info, and schema introspection",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "health_check": {
            "description": "Check server health status",
            "required_params": [],
            "optional_params": [],
        },
        "get_server_info": {
            "description": "Get server information",
            "required_params": [],
            "optional_params": [],
        },
        "schema": {
            "description": "Get input/output schema for a meta-tool",
            "required_params": [],
            "optional_params": ["tool_name"],
        },
    },
)


async def workday_system(request: SystemInput) -> MetaToolOutput:
    """System utilities: health check, server info, and schema introspection."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=SYSTEM_HELP)
        case "health_check":
            result = await workday_health_check()
            return _make_output("health_check", result)
        case "get_server_info":
            return _make_output(
                "get_server_info",
                {"name": "Workday", "version": "1.0.0", "status": "running"},
            )
        case "schema":
            if request.tool_name is not None:
                if request.tool_name not in TOOL_SCHEMAS:
                    return _make_output("schema", error=f"Unknown tool: {request.tool_name}")
                schema_info = TOOL_SCHEMAS[request.tool_name]
                return _make_output(
                    "schema",
                    {
                        "tool_name": request.tool_name,
                        "input_schema": schema_info["input"].model_json_schema(),
                        "output_schema": schema_info["output"].model_json_schema(),
                    },
                )
            else:
                return _make_output("schema", {"available_tools": list(TOOL_SCHEMAS.keys())})
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Help Meta-Tool (Workday Help module)
# =============================================================================

HELP_HELP = HelpResponse(
    tool_name="workday_help",
    description="Help desk case management: create/manage cases, timeline, messages, attachments, audit",
    actions={
        "help": {
            "description": "Show this help information",
            "required_params": [],
            "optional_params": [],
        },
        "create_case": {
            "description": "Create a new help desk case",
            "required_params": ["case_type", "owner", "candidate_identifier"],
            "optional_params": [
                "case_id",
                "status",
                "due_date",
                "metadata",
                "actor",
                "actor_persona",
            ],
        },
        "get_case": {
            "description": "Retrieve a case by ID",
            "required_params": ["case_id"],
            "optional_params": ["actor", "actor_persona"],
        },
        "update_status": {
            "description": "Update case status with state machine validation",
            "required_params": ["case_id", "current_status", "new_status", "rationale"],
            "optional_params": ["actor", "actor_persona"],
        },
        "reassign_owner": {
            "description": "Reassign case to a new owner",
            "required_params": ["case_id", "new_owner", "rationale"],
            "optional_params": ["actor", "actor_persona"],
        },
        "update_due_date": {
            "description": "Update case due date",
            "required_params": ["case_id", "new_due_date", "rationale"],
            "optional_params": ["actor", "actor_persona"],
        },
        "search_cases": {
            "description": "Search cases with filters",
            "required_params": [],
            "optional_params": [
                "status",
                "owner",
                "candidate_identifier",
                "created_after",
                "created_before",
                "cursor",
                "limit",
                "actor",
                "actor_persona",
            ],
        },
        "add_timeline_event": {
            "description": "Add an immutable timeline event",
            "required_params": ["case_id", "event_type", "actor"],
            "optional_params": ["notes", "metadata"],
        },
        "get_timeline_events": {
            "description": "Get timeline events for a case",
            "required_params": ["case_id"],
            "optional_params": ["cursor", "limit"],
        },
        "get_timeline_snapshot": {
            "description": "Get complete case snapshot (case, timeline, messages, attachments)",
            "required_params": ["case_id"],
            "optional_params": ["as_of_date"],
        },
        "add_message": {
            "description": "Add a message to a case",
            "required_params": ["case_id", "direction", "sender", "body"],
            "optional_params": ["audience", "metadata", "actor", "actor_persona"],
        },
        "search_messages": {
            "description": "Search messages with filters",
            "required_params": [],
            "optional_params": [
                "message_id",
                "case_id",
                "direction",
                "sender",
                "created_after",
                "created_before",
                "cursor",
                "limit",
            ],
        },
        "add_attachment": {
            "description": "Add attachment metadata to a case",
            "required_params": ["case_id", "filename", "uploader"],
            "optional_params": [
                "mime_type",
                "size_bytes",
                "source",
                "external_reference",
                "metadata",
                "actor_persona",
            ],
        },
        "list_attachments": {
            "description": "List attachments for a case",
            "required_params": ["case_id"],
            "optional_params": ["cursor", "limit", "actor_persona"],
        },
        "query_audit": {
            "description": "Query audit history with filters",
            "required_params": [],
            "optional_params": [
                "case_id",
                "actor",
                "action_type",
                "created_after",
                "created_before",
                "cursor",
                "limit",
            ],
        },
    },
)


async def workday_help(request: HelpInput) -> MetaToolOutput:
    """Help desk case management: create/manage cases, timeline, messages, attachments, audit."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=HELP_HELP)
        case "create_case":
            _check_scope("case:write")
            if request.case_id is None:
                return _make_output("create_case", error="case_id required")
            if request.case_type is None:
                return _make_output("create_case", error="case_type required")
            if request.owner is None:
                return _make_output("create_case", error="owner required")
            if request.candidate_identifier is None:
                return _make_output("create_case", error="candidate_identifier required")
            tool_input = CreateCaseRequest(
                case_id=request.case_id,
                case_type=request.case_type,
                owner=request.owner,
                status=request.status or "Open",
                candidate_identifier=request.candidate_identifier,
                due_date=request.due_date,
                metadata=request.metadata,
                actor=request.actor,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_cases_create(tool_input)
            return _make_output("create_case", result)
        case "get_case":
            _check_scope("case:read")
            if request.case_id is None:
                return _make_output("get_case", error="case_id required")
            tool_input = GetCaseRequest(
                case_id=request.case_id,
                actor=request.actor,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_cases_get(tool_input)
            return _make_output("get_case", result)
        case "update_status":
            _check_scope("case:write")
            if request.case_id is None:
                return _make_output("update_status", error="case_id required")
            if request.current_status is None:
                return _make_output("update_status", error="current_status required")
            if request.new_status is None:
                return _make_output("update_status", error="new_status required")
            if request.rationale is None:
                return _make_output("update_status", error="rationale required")
            tool_input = UpdateCaseStatusRequest(
                case_id=request.case_id,
                current_status=request.current_status,
                new_status=request.new_status,
                rationale=request.rationale,
                actor=request.actor,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_cases_update_status(tool_input)
            return _make_output("update_status", result)
        case "reassign_owner":
            _check_scope("case:write")
            if request.case_id is None:
                return _make_output("reassign_owner", error="case_id required")
            if request.new_owner is None:
                return _make_output("reassign_owner", error="new_owner required")
            if request.rationale is None:
                return _make_output("reassign_owner", error="rationale required")
            tool_input = ReassignCaseOwnerRequest(
                case_id=request.case_id,
                new_owner=request.new_owner,
                rationale=request.rationale,
                actor=request.actor,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_cases_reassign_owner(tool_input)
            return _make_output("reassign_owner", result)
        case "update_due_date":
            _check_scope("case:write")
            if request.case_id is None:
                return _make_output("update_due_date", error="case_id required")
            if request.new_due_date is None:
                return _make_output("update_due_date", error="new_due_date required")
            if request.rationale is None:
                return _make_output("update_due_date", error="rationale required")
            tool_input = UpdateCaseDueDateRequest(
                case_id=request.case_id,
                new_due_date=request.new_due_date,
                rationale=request.rationale,
                actor=request.actor,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_cases_update_due_date(tool_input)
            return _make_output("update_due_date", result)
        case "search_cases":
            _check_scope("case:read")
            statuses = [request.status] if request.status else None
            tool_input = SearchCasesRequest(
                status=statuses,
                owner=request.owner,
                candidate_identifier=request.candidate_identifier,
                created_after=request.created_after,
                created_before=request.created_before,
                cursor=request.cursor,
                limit=request.limit or 50,
                actor=request.actor,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_cases_search(tool_input)
            return _make_output("search_cases", result)
        case "add_timeline_event":
            _check_scope("timeline:write")
            if request.case_id is None:
                return _make_output("add_timeline_event", error="case_id required")
            if request.event_type is None:
                return _make_output("add_timeline_event", error="event_type required")
            if request.actor is None:
                return _make_output("add_timeline_event", error="actor required")
            tool_input = AddTimelineEventRequest(
                case_id=request.case_id,
                event_type=request.event_type,
                actor=request.actor,
                notes=request.notes,
                metadata=request.metadata,
            )
            result = await workday_help_timeline_add_event(tool_input)
            return _make_output("add_timeline_event", result)
        case "get_timeline_events":
            _check_scope("timeline:read")
            if request.case_id is None:
                return _make_output("get_timeline_events", error="case_id required")
            tool_input = GetTimelineEventsRequest(
                case_id=request.case_id,
                cursor=request.cursor,
                limit=request.limit or 100,
            )
            result = await workday_help_timeline_get_events(tool_input)
            return _make_output("get_timeline_events", result)
        case "get_timeline_snapshot":
            _check_scope("timeline:read")
            if request.case_id is None:
                return _make_output("get_timeline_snapshot", error="case_id required")
            tool_input = GetTimelineSnapshotRequest(
                case_id=request.case_id,
                as_of_date=request.as_of_date,
            )
            result = await workday_help_timeline_get_snapshot(tool_input)
            return _make_output("get_timeline_snapshot", result)
        case "add_message":
            _check_scope("message:write")
            if request.case_id is None:
                return _make_output("add_message", error="case_id required")
            if request.direction is None:
                return _make_output("add_message", error="direction required")
            if request.sender is None:
                return _make_output("add_message", error="sender required")
            if request.body is None:
                return _make_output("add_message", error="body required")
            if request.actor is None:
                return _make_output("add_message", error="actor required")
            tool_input = AddMessageRequest(
                case_id=request.case_id,
                direction=request.direction,
                sender=request.sender,
                body=request.body,
                audience=request.audience,
                metadata=request.metadata,
                actor=request.actor,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_messages_add(tool_input)
            return _make_output("add_message", result)
        case "search_messages":
            _check_scope("message:read")
            tool_input = SearchMessagesRequest(
                message_id=request.message_id,
                case_id=request.case_id,
                direction=request.direction,
                sender=request.sender,
                created_after=request.created_after,
                created_before=request.created_before,
                cursor=request.cursor,
                limit=request.limit or 50,
            )
            result = await workday_help_messages_search(tool_input)
            return _make_output("search_messages", result)
        case "add_attachment":
            _check_scope("attachment:write")
            if request.case_id is None:
                return _make_output("add_attachment", error="case_id required")
            if request.filename is None:
                return _make_output("add_attachment", error="filename required")
            if request.uploader is None:
                return _make_output("add_attachment", error="uploader required")
            tool_input = AddAttachmentRequest(
                case_id=request.case_id,
                filename=request.filename,
                uploader=request.uploader,
                mime_type=request.mime_type,
                size_bytes=request.size_bytes,
                source=request.source,
                external_reference=request.external_reference,
                metadata=request.metadata,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_attachments_add(tool_input)
            return _make_output("add_attachment", result)
        case "list_attachments":
            _check_scope("attachment:read")
            if request.case_id is None:
                return _make_output("list_attachments", error="case_id required")
            tool_input = ListAttachmentsRequest(
                case_id=request.case_id,
                cursor=request.cursor,
                limit=request.limit or 50,
                actor_persona=request.actor_persona,
            )
            result = await workday_help_attachments_list(tool_input)
            return _make_output("list_attachments", result)
        case "query_audit":
            _check_scope("audit:read")
            tool_input = QueryAuditHistoryRequest(
                case_id=request.case_id,
                actor=request.actor,
                action_type=request.action_type,
                created_after=request.created_after,
                created_before=request.created_before,
                cursor=request.cursor,
                limit=request.limit or 100,
            )
            result = await workday_help_audit_query_history(tool_input)
            return _make_output("query_audit", result)
    raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# Registration
# =============================================================================


def register_meta_tools(mcp: FastMCP) -> None:
    """Register all meta-tools with the MCP server."""
    mcp.tool()(workday_workers)
    mcp.tool()(workday_positions)
    mcp.tool()(workday_organizations)
    mcp.tool()(workday_job_profiles)
    mcp.tool()(workday_cases)
    mcp.tool()(workday_hcm)
    mcp.tool()(workday_milestones)
    mcp.tool()(workday_tasks)
    mcp.tool()(workday_policies)
    mcp.tool()(workday_reports)
    mcp.tool()(workday_exceptions)
    mcp.tool()(workday_audit)
    mcp.tool()(workday_system)
    mcp.tool()(workday_help)
