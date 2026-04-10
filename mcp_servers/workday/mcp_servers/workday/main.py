import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)

# Add scripts directory to path for database tools
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
from db_tools import create_database_tools  # noqa: E402
from mcp_auth import public_tool  # noqa: E402
from mcp_middleware import ServerConfig, apply_default_setup, run_server  # noqa: E402
from middleware.logging import LoggingMiddleware  # noqa: E402

# Audit tools (V2)
from tools.audit_tools import workday_audit_get_history  # noqa: E402

# Case tools (V2 Pre-Onboarding)
from tools.case_tools import (  # noqa: E402
    workday_assign_owner_case,
    workday_create_case,
    workday_get_case,
    workday_search_case,
    workday_snapshot_case,
    workday_update_case,
)

# Cost center tools
from tools.cost_center_tools import workday_create_cost_center  # noqa: E402

# Exception tools
from tools.exception_tools import (  # noqa: E402
    workday_exception_approve,
    workday_exception_request,
)

# HCM tools (read and write-back)
from tools.hcm_tools import (  # noqa: E402
    workday_hcm_confirm_start_date,
    workday_hcm_read_context,
    workday_hcm_read_position,
    workday_hcm_update_readiness,
)

# Health tools
from tools.health_tools import workday_health_check  # noqa: E402
from tools.help.attachments import (  # noqa: E402
    workday_help_attachments_add,
    workday_help_attachments_list,
)
from tools.help.audit import workday_help_audit_query_history  # noqa: E402

# Help tools (Workday Help module)
from tools.help.cases import (  # noqa: E402
    workday_help_cases_create,
    workday_help_cases_get,
    workday_help_cases_reassign_owner,
    workday_help_cases_search,
    workday_help_cases_update_due_date,
    workday_help_cases_update_status,
)
from tools.help.messages import (  # noqa: E402
    workday_help_messages_add,
    workday_help_messages_search,
)
from tools.help.timeline import (  # noqa: E402
    workday_help_timeline_add_event,
    workday_help_timeline_get_events,
    workday_help_timeline_get_snapshot,
)

# Job profile tools
from tools.job_profile_tools import (  # noqa: E402
    workday_create_job_profile,
    workday_get_job_profile,
    workday_list_job_profiles,
)

# Location tools
from tools.location_tools import workday_create_location  # noqa: E402

# Milestone tools (V2)
from tools.milestone_tools import (  # noqa: E402
    workday_milestones_list,
    workday_milestones_update,
)

# Organization tools
from tools.org_tools import (  # noqa: E402
    workday_create_org,
    workday_get_org,
    workday_get_org_hierarchy,
    workday_list_orgs,
)

# Policy tools
from tools.policy_tools import (  # noqa: E402
    workday_policies_attach_to_case,
    workday_policies_create,
    workday_policies_create_payroll_cutoff,
    workday_policies_get_applicable,
)

# Position tools
from tools.position_tools import (  # noqa: E402
    workday_close_position,
    workday_create_position,
    workday_get_position,
    workday_list_positions,
)

# Report tools
from tools.report_tools import (  # noqa: E402
    workday_report_headcount,
    workday_report_movements,
    workday_report_org_hierarchy,
    workday_report_positions,
    workday_report_workforce_roster,
)

# Task tools (V2)
from tools.task_tools import workday_tasks_create, workday_tasks_update  # noqa: E402

# Worker tools
from tools.worker_tools import (  # noqa: E402
    workday_get_worker,
    workday_hire_worker,
    workday_list_workers,
    workday_terminate_worker,
    workday_transfer_worker,
)

# Server configuration
SERVER_CONFIG = ServerConfig(
    name="workday-mcp",
    version="1.0.0",
    description="Workday HCM MCP Server for HR automation workflows",
    features={
        "personas": ["case_owner", "hr_admin", "manager", "hr_analyst"],
        "persistence": "sqlite",
    },
)

mcp = FastMCP(
    name=SERVER_CONFIG.name,
    instructions=SERVER_CONFIG.description,
    version=SERVER_CONFIG.version,
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())


def _register_tools():
    """Register all tools with the MCP server.

    Called from main() to ensure tools are registered before auth setup.
    Supports dual registration pattern:
    - GUI_ENABLED=false: Register meta-tools (for LLMs, fewer tokens) [default]
    - GUI_ENABLED=true: Register individual tools (for UI/humans)
    """
    gui_enabled = os.getenv("GUI_ENABLED", "").lower()
    use_gui_tools = gui_enabled in ("true", "1", "yes")

    if use_gui_tools:
        # ======================================================================
        # UI Mode: Register individual tools (for humans)
        # ======================================================================
        enabled_tools = os.getenv("TOOLS", "").split(",")
        enabled_tools = [t.strip() for t in enabled_tools if t.strip()]

        # Worker tools
        if not enabled_tools or "workday_hire_worker" in enabled_tools:
            mcp.tool(workday_hire_worker)
        if not enabled_tools or "workday_get_worker" in enabled_tools:
            mcp.tool(workday_get_worker)
        if not enabled_tools or "workday_list_workers" in enabled_tools:
            mcp.tool(workday_list_workers)
        if not enabled_tools or "workday_transfer_worker" in enabled_tools:
            mcp.tool(workday_transfer_worker)
        if not enabled_tools or "workday_terminate_worker" in enabled_tools:
            mcp.tool(workday_terminate_worker)

        # Position tools
        if not enabled_tools or "workday_get_position" in enabled_tools:
            mcp.tool(workday_get_position)
        if not enabled_tools or "workday_create_position" in enabled_tools:
            mcp.tool(workday_create_position)
        if not enabled_tools or "workday_list_positions" in enabled_tools:
            mcp.tool(workday_list_positions)
        if not enabled_tools or "workday_close_position" in enabled_tools:
            mcp.tool(workday_close_position)

        # Organization tools
        if not enabled_tools or "workday_get_org" in enabled_tools:
            mcp.tool(workday_get_org)
        if not enabled_tools or "workday_list_orgs" in enabled_tools:
            mcp.tool(workday_list_orgs)
        if not enabled_tools or "workday_get_org_hierarchy" in enabled_tools:
            mcp.tool(workday_get_org_hierarchy)
        if not enabled_tools or "workday_create_org" in enabled_tools:
            mcp.tool(workday_create_org)
        if not enabled_tools or "workday_create_cost_center" in enabled_tools:
            mcp.tool(workday_create_cost_center)
        if not enabled_tools or "workday_create_location" in enabled_tools:
            mcp.tool(workday_create_location)

        # Job profile tools
        if not enabled_tools or "workday_get_job_profile" in enabled_tools:
            mcp.tool(workday_get_job_profile)
        if not enabled_tools or "workday_list_job_profiles" in enabled_tools:
            mcp.tool(workday_list_job_profiles)
        if not enabled_tools or "workday_create_job_profile" in enabled_tools:
            mcp.tool(workday_create_job_profile)

        # Report tools
        if not enabled_tools or "workday_report_workforce_roster" in enabled_tools:
            mcp.tool(workday_report_workforce_roster)
        if not enabled_tools or "workday_report_headcount" in enabled_tools:
            mcp.tool(workday_report_headcount)
        if not enabled_tools or "workday_report_movements" in enabled_tools:
            mcp.tool(workday_report_movements)
        if not enabled_tools or "workday_report_positions" in enabled_tools:
            mcp.tool(workday_report_positions)
        if not enabled_tools or "workday_report_org_hierarchy" in enabled_tools:
            mcp.tool(workday_report_org_hierarchy)

        # Exception tools
        if not enabled_tools or "workday_exception_request" in enabled_tools:
            mcp.tool(workday_exception_request)
        if not enabled_tools or "workday_exception_approve" in enabled_tools:
            mcp.tool(workday_exception_approve)

        # Audit tools
        if not enabled_tools or "workday_audit_get_history" in enabled_tools:
            mcp.tool(workday_audit_get_history)

        # Case tools
        if not enabled_tools or "workday_create_case" in enabled_tools:
            mcp.tool(workday_create_case)
        if not enabled_tools or "workday_get_case" in enabled_tools:
            mcp.tool(workday_get_case)
        if not enabled_tools or "workday_update_case" in enabled_tools:
            mcp.tool(workday_update_case)
        if not enabled_tools or "workday_assign_owner_case" in enabled_tools:
            mcp.tool(workday_assign_owner_case)
        if not enabled_tools or "workday_search_case" in enabled_tools:
            mcp.tool(workday_search_case)
        if not enabled_tools or "workday_snapshot_case" in enabled_tools:
            mcp.tool(workday_snapshot_case)

        # Milestone tools
        if not enabled_tools or "workday_milestones_list" in enabled_tools:
            mcp.tool(workday_milestones_list)
        if not enabled_tools or "workday_milestones_update" in enabled_tools:
            mcp.tool(workday_milestones_update)

        # Task tools
        if not enabled_tools or "workday_tasks_create" in enabled_tools:
            mcp.tool(workday_tasks_create)
        if not enabled_tools or "workday_tasks_update" in enabled_tools:
            mcp.tool(workday_tasks_update)

        # Health tools
        if not enabled_tools or "workday_health_check" in enabled_tools:
            mcp.tool(workday_health_check)

        # HCM tools
        if not enabled_tools or "workday_hcm_read_context" in enabled_tools:
            mcp.tool(workday_hcm_read_context)
        if not enabled_tools or "workday_hcm_read_position" in enabled_tools:
            mcp.tool(workday_hcm_read_position)
        if not enabled_tools or "workday_hcm_confirm_start_date" in enabled_tools:
            mcp.tool(workday_hcm_confirm_start_date)
        if not enabled_tools or "workday_hcm_update_readiness" in enabled_tools:
            mcp.tool(workday_hcm_update_readiness)

        # Policy tools
        if not enabled_tools or "workday_policies_get_applicable" in enabled_tools:
            mcp.tool(workday_policies_get_applicable)
        if not enabled_tools or "workday_policies_attach_to_case" in enabled_tools:
            mcp.tool(workday_policies_attach_to_case)
        if not enabled_tools or "workday_policies_create" in enabled_tools:
            mcp.tool(workday_policies_create)
        if not enabled_tools or "workday_policies_create_payroll_cutoff" in enabled_tools:
            mcp.tool(workday_policies_create_payroll_cutoff)

        # Help Case tools
        if not enabled_tools or "workday_help_cases_create" in enabled_tools:
            mcp.tool(workday_help_cases_create)
        if not enabled_tools or "workday_help_cases_get" in enabled_tools:
            mcp.tool(workday_help_cases_get)
        if not enabled_tools or "workday_help_cases_update_status" in enabled_tools:
            mcp.tool(workday_help_cases_update_status)
        if not enabled_tools or "workday_help_cases_reassign_owner" in enabled_tools:
            mcp.tool(workday_help_cases_reassign_owner)
        if not enabled_tools or "workday_help_cases_update_due_date" in enabled_tools:
            mcp.tool(workday_help_cases_update_due_date)
        if not enabled_tools or "workday_help_cases_search" in enabled_tools:
            mcp.tool(workday_help_cases_search)

        # Help Timeline tools
        if not enabled_tools or "workday_help_timeline_add_event" in enabled_tools:
            mcp.tool(workday_help_timeline_add_event)
        if not enabled_tools or "workday_help_timeline_get_events" in enabled_tools:
            mcp.tool(workday_help_timeline_get_events)
        if not enabled_tools or "workday_help_timeline_get_snapshot" in enabled_tools:
            mcp.tool(workday_help_timeline_get_snapshot)

        # Help Message tools
        if not enabled_tools or "workday_help_messages_add" in enabled_tools:
            mcp.tool(workday_help_messages_add)
        if not enabled_tools or "workday_help_messages_search" in enabled_tools:
            mcp.tool(workday_help_messages_search)

        # Help Attachment tools
        if not enabled_tools or "workday_help_attachments_add" in enabled_tools:
            mcp.tool(workday_help_attachments_add)
        if not enabled_tools or "workday_help_attachments_list" in enabled_tools:
            mcp.tool(workday_help_attachments_list)

        # Help Audit tools
        if not enabled_tools or "workday_help_audit_query_history" in enabled_tools:
            mcp.tool(workday_help_audit_query_history)

        # Database Management Tools
        create_database_tools(mcp, "db.session", public_tool)

    else:
        # ======================================================================
        # LLM Mode: Register meta-tools (default)
        # ======================================================================
        from tools._meta_tools import register_meta_tools

        register_meta_tools(mcp)


def main():
    """Entry point for the Workday MCP server."""
    # Register all tools before auth setup
    _register_tools()
    # Canonical schema-compat path from shared middleware package.
    apply_default_setup(mcp)

    # Run the server (handles server_info and auth setup based on ENABLE_AUTH env var)
    run_server(mcp, config=SERVER_CONFIG)


if __name__ == "__main__":
    main()
