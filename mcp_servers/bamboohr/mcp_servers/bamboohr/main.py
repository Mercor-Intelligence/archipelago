"""MCP Server: BambooHR

Mock BambooHR API server for testing HR automation workflows.

Features:
- SQLite database (file-based by default, configurable via BAMBOOHR_DB_PATH)
- Persona-based RBAC (hr_admin, manager, employee)
- Full CRUD for employees, time-off requests, metadata
- Dataset querying with BambooHR-compatible filtering

Authentication Flow:
1. Agent calls login(username, password) to get a token
2. Agent includes token in Authorization header: "Bearer <token>"
3. AuthGuard validates token and checks scopes for each tool
4. Tools use @require_scopes() to enforce access control

Database Configuration:
- Default: File-based SQLite at db/bamboohr.db (persists across sessions)
- Set BAMBOOHR_DB_PATH=:memory: for in-memory testing
"""

import os
from contextlib import asynccontextmanager
from typing import Literal

from db import init_db
from fastmcp import FastMCP
from mcp_auth import public_tool, require_roles, require_scopes
from mcp_middleware import ServerConfig, create_database_tools, run_server

# Server configuration
SERVER_CONFIG = ServerConfig(
    name="bamboohr-mcp",
    version="0.1.0",
    description="Mock BambooHR API server for testing HR automation workflows",
)


@asynccontextmanager
async def lifespan(app):
    """Initialize database on server startup."""
    await init_db()
    yield


mcp = FastMCP(
    name=SERVER_CONFIG.name,
    instructions=SERVER_CONFIG.description,
    version=SERVER_CONFIG.version,
    lifespan=lifespan,
)


def _register_tools():
    """Register all tools with the MCP server.

    Called from main() to ensure tools are registered before auth setup.
    Supports dual registration pattern:
    - GUI_ENABLED=false: Register meta-tools (for LLMs, fewer tokens)
    - GUI_ENABLED not set or true: Register individual tools (for UI/humans) [default]
    """
    gui_enabled = os.getenv("GUI_ENABLED", "").lower()
    use_gui_tools = gui_enabled not in ("false", "0", "no")

    if use_gui_tools:
        from tools import (
            assign_policy,
            create_policy,
            create_request,
            create_type,
            estimate_future_balances,
            get_balances,
            get_company_info,
            get_countries,
            get_custom_report,
            get_custom_reports,
            get_dataset_field_options,
            get_dataset_fields,
            get_directory_for_persona,
            get_employee,
            get_employee_policies,
            get_field_options,
            get_fields,
            get_list_fields,
            get_policies,
            get_requests,
            get_states,
            get_types,
            get_users,
            get_whos_out,
            list_datasets,
            query_dataset,
            run_company_report,
            run_custom_report,
            search_employees,
            search_metadata,
            search_time_off,
            update_balance,
            update_employee,
            update_field_options,
            update_request_status,
        )

        # ============================================================================
        # Employee Tools
        # ============================================================================
        @mcp.tool(name="bamboo_employees_create")
        @require_roles("hr_admin")
        @require_scopes("write:employees")
        async def create_employee_tool(  # noqa: N802
            firstName: str,  # noqa: N803
            lastName: str,  # noqa: N803
            preferredName: str | None = None,  # noqa: N803
            middleName: str | None = None,  # noqa: N803
            workEmail: str | None = None,  # noqa: N803
            employeeNumber: str | None = None,  # noqa: N803
            hireDate: str | None = None,  # noqa: N803
            department: str | None = None,
            jobTitle: str | None = None,  # noqa: N803
            location: str | None = None,
            supervisorId: int | None = None,  # noqa: N803
            status: str | None = None,
            workPhone: str | None = None,  # noqa: N803
            mobilePhone: str | None = None,  # noqa: N803
            address1: str | None = None,
            address2: str | None = None,
            city: str | None = None,
            state: str | None = None,
            zipcode: str | None = None,
            country: str | None = None,
            ssn: str | None = None,
            dateOfBirth: str | None = None,  # noqa: N803
            gender: str | None = None,
            maritalStatus: str | None = None,  # noqa: N803
            ethnicity: str | None = None,
            salary: str | None = None,
            payType: str | None = None,  # noqa: N803
            payRate: str | None = None,  # noqa: N803
            payPer: str | None = None,  # noqa: N803
            paySchedule: str | None = None,  # noqa: N803
            linkedIn: str | None = None,  # noqa: N803
            division: str | None = None,
            workPhoneExtension: str | None = None,  # noqa: N803
            homeEmail: str | None = None,  # noqa: N803
            photoUrl: str | None = None,  # noqa: N803
            displayName: str | None = None,  # noqa: N803
            terminationDate: str | None = None,  # noqa: N803
        ) -> dict:
            """Create a new employee record."""
            from tools.create_employee import CreateEmployeeInput, create_employee

            input_data = CreateEmployeeInput(
                firstName=firstName,
                lastName=lastName,
                preferredName=preferredName,
                middleName=middleName,
                workEmail=workEmail,
                employeeNumber=employeeNumber,
                hireDate=hireDate,
                department=department,
                jobTitle=jobTitle,
                location=location,
                supervisorId=supervisorId,
                status=status,
                workPhone=workPhone,
                mobilePhone=mobilePhone,
                address1=address1,
                address2=address2,
                city=city,
                state=state,
                zipcode=zipcode,
                country=country,
                ssn=ssn,
                dateOfBirth=dateOfBirth,
                gender=gender,
                maritalStatus=maritalStatus,
                ethnicity=ethnicity,
                salary=salary,
                payType=payType,
                payRate=payRate,
                payPer=payPer,
                paySchedule=paySchedule,
                linkedIn=linkedIn,
                division=division,
                workPhoneExtension=workPhoneExtension,
                homeEmail=homeEmail,
                photoUrl=photoUrl,
                displayName=displayName,
                terminationDate=terminationDate,
            )

            result = await create_employee(input_data)
            return result.model_dump(by_alias=True)

        @mcp.tool(name="bamboo_employees_get_directory")
        @require_scopes("read:employees")
        async def get_directory(
            fields: list[str] | None = None,
            statusFilter: str | None = None,  # noqa: N803
        ) -> dict:
            """Get employee directory with field definitions and employee records."""
            result = await get_directory_for_persona(
                requested_fields=fields,
                status_filter=statusFilter,
            )
            return result.model_dump(by_alias=True)

        @mcp.tool(name="bamboo_employees_get")
        @require_scopes("read:employees")
        async def get_employee_tool(  # noqa: N802
            employeeId: str,  # noqa: N803
            fields: list[str] | None = None,
        ) -> dict:
            """Get a single employee by ID."""
            return await get_employee(employee_id=employeeId, fields=fields)

        mcp.tool(name="bamboo_employees_update")(update_employee)
        mcp.tool(name="bamboo_employees_get_company_info")(get_company_info)

        # ============================================================================
        # Meta Tools
        # ============================================================================
        @mcp.tool(name="bamboo_meta_get_countries")
        @public_tool
        async def meta_get_countries() -> dict:
            """List all supported countries with ISO codes."""
            return await get_countries()

        @mcp.tool(name="bamboo_meta_get_states")
        @public_tool
        async def meta_get_states(country_code: str) -> dict:
            """List states or provinces for a country."""
            return await get_states(country_code)

        @mcp.tool(name="bamboo_meta_get_list_fields")
        @public_tool
        async def meta_get_list_fields() -> list:
            """Retrieve all list-type fields with their options."""
            return await get_list_fields()

        @mcp.tool(name="bamboo_meta_get_fields")
        @public_tool
        async def meta_get_fields() -> list:
            """Retrieve all standard and custom field definitions."""
            return await get_fields()

        @mcp.tool(name="bamboo_meta_get_users")
        @require_roles("hr_admin", "manager")
        @require_scopes("read:metadata")
        async def meta_get_users() -> list:
            """List all BambooHR users with access to the system."""
            return await get_users(filter_emails_for_non_admins=True)

        @mcp.tool(name="bamboo_meta_get_field_options")
        @public_tool
        async def meta_get_field_options(field_id: str) -> list:
            """Retrieve options for a specific list field."""
            return await get_field_options(field_id)

        mcp.tool(name="bamboo_meta_update_field_options")(update_field_options)

        # ============================================================================
        # Report Tools
        # ============================================================================
        mcp.tool(name="bamboo_reports_run_company_report")(run_company_report)
        mcp.tool(name="bamboo_reports_get_custom_reports")(get_custom_reports)
        mcp.tool(name="bamboo_reports_get_custom_report")(get_custom_report)
        mcp.tool(name="bamboo_reports_run_custom_report")(run_custom_report)

        # ============================================================================
        # Dataset Tools
        # ============================================================================
        @mcp.tool(name="bamboo_datasets_list")
        @public_tool
        async def datasets_list() -> dict:
            """List all available datasets."""
            return await list_datasets()

        @mcp.tool(name="bamboo_datasets_get_fields")
        @public_tool
        async def datasets_get_fields(dataset_name: str) -> list:
            """Get field definitions for a specific dataset."""
            return await get_dataset_fields(dataset_name)

        @mcp.tool(name="bamboo_datasets_get_field_options")
        @public_tool
        async def datasets_get_field_options(dataset_name: str, field_id: str) -> list:
            """Get option values for a dataset field."""
            return await get_dataset_field_options(dataset_name, field_id)

        @mcp.tool(name="bamboo_datasets_query")
        @require_roles("hr_admin")
        @require_scopes("read:employees")
        async def datasets_query(
            dataset_name: str,
            fields: list[str],
            filters: list[dict] | None = None,
            sort_by: list[dict] | None = None,
            group_by: list[str] | None = None,
            aggregations: list[dict] | None = None,
            matches: Literal["all", "any"] = "all",
        ) -> dict:
            """Query a dataset with filtering, sorting, grouping, and aggregations."""
            return await query_dataset(
                dataset_name=dataset_name,
                fields=fields,
                filters=filters,
                sort_by=sort_by,
                group_by=group_by,
                aggregations=aggregations,
                matches=matches,
            )

        # ============================================================================
        # Time-Off Tools
        # ============================================================================
        mcp.tool(name="bamboo_time_off_create_type")(create_type)

        @mcp.tool(name="bamboo_time_off_get_types")
        @public_tool
        async def time_off_get_types(mode: str | None = None) -> dict:
            """Get time-off types with default hours configuration."""
            return await get_types(mode=mode)

        # Time-Off Policy Tools
        mcp.tool(name="bamboo_time_off_get_policies")(get_policies)
        mcp.tool(name="bamboo_time_off_create_policy")(create_policy)
        mcp.tool(name="bamboo_time_off_get_employee_policies")(get_employee_policies)
        mcp.tool(name="bamboo_time_off_assign_policy")(assign_policy)

        # Time-Off Balance Tools
        mcp.tool(name="bamboo_time_off_get_balances")(get_balances)
        mcp.tool(name="bamboo_time_off_update_balance")(update_balance)
        mcp.tool(name="bamboo_time_off_estimate_future_balances")(estimate_future_balances)
        mcp.tool(name="bamboo_time_off_get_whos_out")(get_whos_out)

        # Time-Off Request Tools
        mcp.tool(name="bamboo_time_off_get_requests")(get_requests)
        mcp.tool(name="bamboo_time_off_create_request")(create_request)
        mcp.tool(name="bamboo_time_off_update_request_status")(update_request_status)

        # ============================================================================
        # Search Tools
        # ============================================================================
        mcp.tool(name="bamboo_search_employees")(search_employees)
        mcp.tool(name="bamboo_search_time_off")(search_time_off)
        mcp.tool(name="bamboo_search_metadata")(search_metadata)

        # ============================================================================
        # Utility Tools
        # ============================================================================
        @mcp.tool(name="bamboo_reset_state")
        @require_roles("hr_admin")
        @require_scopes("admin:system")
        async def reset_state_tool(confirm: bool) -> dict:
            """Reset database to empty state for testing purposes."""
            from tools.reset_state import ResetStateInput, reset_state

            input_data = ResetStateInput(confirm=confirm)
            result = await reset_state(input_data)
            return result.model_dump(by_alias=True)

        # Database Management Tools
        create_database_tools(mcp, "db.session", public_tool)

    else:
        from tools._meta_tools import (
            bamboo_admin,
            bamboo_datasets,
            bamboo_employees,
            bamboo_metadata,
            bamboo_reports,
            bamboo_schema,
            bamboo_search,
            bamboo_time_off,
        )

        # META-TOOLS (LLM-optimized, 8 tools with action-parameter routing)
        mcp.tool(name="bamboo_admin")(public_tool(bamboo_admin))
        mcp.tool(name="bamboo_metadata")(public_tool(bamboo_metadata))
        mcp.tool(name="bamboo_search")(require_scopes("read:employees")(bamboo_search))
        mcp.tool(name="bamboo_datasets")(require_scopes("read:employees")(bamboo_datasets))
        mcp.tool(name="bamboo_reports")(require_scopes("read:employees")(bamboo_reports))
        mcp.tool(name="bamboo_employees")(require_scopes("read:employees")(bamboo_employees))
        mcp.tool(name="bamboo_time_off")(require_scopes("read:time_off")(bamboo_time_off))
        mcp.tool(name="bamboo_schema")(public_tool(bamboo_schema))


def main():
    """Entry point for bamboohr-mcp package script."""
    # Register all tools before auth setup
    _register_tools()

    # Run the server (handles server_info and auth setup)
    run_server(mcp, config=SERVER_CONFIG)


if __name__ == "__main__":
    main()
