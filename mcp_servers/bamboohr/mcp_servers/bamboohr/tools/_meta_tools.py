"""Meta-tools for BambooHR MCP server.

This module implements 8 domain meta-tools with action-parameter routing.
The underscore prefix hides these from the UI scanner while exposing them to LLMs.

Meta-tools reduce LLM context by ~78% (36 tools → 8 meta-tools).

Individual tools are preserved for UI forms via dual registration in main.py.

IMPORTANT: Meta-tools must enforce permissions internally since they bypass the
MCP framework's decorator-based permission enforcement. Each action that requires
elevated permissions must check roles/scopes before calling the underlying function.
"""

from typing import Any

from mcp_auth import user_has_role, user_has_scope
from schemas.meta_tools import (
    ADMIN_HELP,
    DATASETS_HELP,
    EMPLOYEES_HELP,
    METADATA_HELP,
    REPORTS_HELP,
    SEARCH_HELP,
    TIME_OFF_HELP,
    AdminInput,
    DatasetsInput,
    EmployeesInput,
    MetadataInput,
    MetaToolOutput,
    ReportsInput,
    SchemaInput,
    SchemaOutput,
    SearchInput,
    TimeOffInput,
)

# Import individual tools
from .create_employee import CreateEmployeeInput, create_employee
from .datasets import (
    get_dataset_field_options,
    get_dataset_fields,
    list_datasets,
    query_dataset,
)
from .employees import get_company_info, get_employee, update_employee
from .estimate_future_balances import estimate_future_balances
from .get_directory import get_directory_for_persona
from .meta import (
    get_countries,
    get_field_options,
    get_fields,
    get_list_fields,
    get_states,
    get_users,
    update_field_options,
)
from .reports import (
    get_custom_report,
    get_custom_reports,
    run_company_report,
    run_custom_report,
)
from .reset_state import ResetStateInput, reset_state
from .search import search_employees, search_metadata, search_time_off
from .time_off import get_types
from .time_off_balances import get_balances, update_balance
from .time_off_policies import assign_policy, get_employee_policies, get_policies
from .time_off_requests import create_request, get_requests, update_request_status
from .whos_out import get_whos_out


# =============================================================================
# Permission check helpers
# =============================================================================
def _check_role(required_role: str) -> None:
    """Check if current user has required role. Raises PermissionError if not.

    Note: user_has_role returns True if auth is disabled, granting full access.
    """
    if not user_has_role(required_role):
        raise PermissionError(f"Access denied: Requires role '{required_role}'")


def _check_scope(required_scope: str) -> None:
    """Check if current user has required scope. Raises PermissionError if not.

    Note: user_has_scope returns True if auth is disabled, granting full access.
    """
    if not user_has_scope(required_scope):
        raise PermissionError(f"Access denied: Missing scope '{required_scope}'")


def _make_output(action: str, result: dict) -> MetaToolOutput:
    """Create MetaToolOutput, detecting error responses from underlying functions."""
    if isinstance(result, dict) and "error" in result:
        # Underlying function returned an error dict - extract error message
        error_info = result["error"]
        if isinstance(error_info, dict):
            error_msg = error_info.get("message", str(error_info))
        else:
            error_msg = str(error_info)
        return MetaToolOutput(action=action, error=error_msg)
    return MetaToolOutput(action=action, data=result)


# =============================================================================
# bamboo_admin (2 actions)
# =============================================================================
async def bamboo_admin(request: AdminInput) -> MetaToolOutput:
    """Administrative operations for BambooHR server."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=ADMIN_HELP)

        case "server_info":
            return MetaToolOutput(
                action="server_info",
                data={
                    "name": "BambooHR MCP Server",
                    "version": "0.1.0",
                    "status": "running",
                    "description": "Mock BambooHR API for testing HR automation workflows",
                    "features": {
                        "employees": "active",
                        "time_off": "partial",
                        "metadata": "partial",
                        "reports": "active",
                        "datasets": "active",
                    },
                },
            )

        case "reset_state":
            _check_role("hr_admin")  # reset_state requires hr_admin role
            if request.confirm is None:
                raise ValueError("confirm parameter is required for reset_state action")
            input_data = ResetStateInput(confirm=request.confirm)
            result = await reset_state(input_data)
            return MetaToolOutput(action="reset_state", data=result.model_dump(by_alias=True))

        case _:
            raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# bamboo_metadata (7 actions)
# =============================================================================
async def bamboo_metadata(request: MetadataInput) -> MetaToolOutput:
    """Access system metadata: countries, states, fields, and users."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=METADATA_HELP)

        case "countries":
            result = await get_countries()
            return MetaToolOutput(action="countries", data=result)

        case "states":
            if not request.country_code:
                raise ValueError("countryCode is required for states action")
            result = await get_states(request.country_code)
            return MetaToolOutput(action="states", data=result)

        case "fields":
            result = await get_fields()
            return MetaToolOutput(action="fields", data=result)

        case "list_fields":
            result = await get_list_fields()
            return MetaToolOutput(action="list_fields", data=result)

        case "field_options":
            if not request.field_id:
                raise ValueError("fieldId is required for field_options action")
            result = await get_field_options(request.field_id)
            return MetaToolOutput(action="field_options", data=result)

        case "update_options":
            _check_scope("write:metadata")  # update_options requires write:metadata scope
            if not request.list_field_id:
                raise ValueError("listFieldId is required for update_options action")
            if not request.options:
                raise ValueError("options is required for update_options action")
            result = await update_field_options(request.list_field_id, request.options)
            return MetaToolOutput(action="update_options", data=result)

        case "users":
            # users action requires hr_admin or manager role
            # user_has_role returns True if auth disabled, granting full access
            if not user_has_role("hr_admin") and not user_has_role("manager"):
                raise PermissionError("Access denied: Requires role 'hr_admin' or 'manager'")
            result = await get_users(filter_emails_for_non_admins=True)
            return MetaToolOutput(action="users", data=result)

        case _:
            raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# bamboo_search (3 actions)
# =============================================================================
async def bamboo_search(request: SearchInput) -> MetaToolOutput:
    """Search across employees, time-off, and metadata with fuzzy matching."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=SEARCH_HELP)

        case "employees":
            result = await search_employees(
                query=request.query or "",
                fields=request.fields,
                filters=request.filters,
                page=request.page,
                page_size=request.page_size,
            )
            return MetaToolOutput(action="employees", data=result)

        case "time_off":
            result = await search_time_off(
                query=request.query or "",
                fields=request.fields,
                filters=request.filters,
                page=request.page,
                page_size=request.page_size,
            )
            return MetaToolOutput(action="time_off", data=result)

        case "metadata":
            result = await search_metadata(
                query=request.query or "",
                entity_types=request.entity_types,
                page=request.page,
                page_size=request.page_size,
            )
            return MetaToolOutput(action="metadata", data=result)

        case _:
            raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# bamboo_datasets (4 actions)
# =============================================================================
async def bamboo_datasets(request: DatasetsInput) -> MetaToolOutput:
    """Query BambooHR datasets with filtering, sorting, and aggregations."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=DATASETS_HELP)

        case "list":
            result = await list_datasets()
            return MetaToolOutput(action="list", data=result)

        case "fields":
            if not request.dataset_name:
                raise ValueError("datasetName is required for fields action")
            result = await get_dataset_fields(request.dataset_name)
            return MetaToolOutput(action="fields", data=result)

        case "field_options":
            if not request.dataset_name:
                raise ValueError("datasetName is required for field_options action")
            if not request.field_id:
                raise ValueError("fieldId is required for field_options action")
            result = await get_dataset_field_options(request.dataset_name, request.field_id)
            return MetaToolOutput(action="field_options", data=result)

        case "query":
            _check_role("hr_admin")  # query action requires hr_admin role
            if not request.dataset_name:
                raise ValueError("datasetName is required for query action")
            if not request.fields:
                raise ValueError("fields is required for query action")
            result = await query_dataset(
                dataset_name=request.dataset_name,
                fields=request.fields,
                filters=request.filters,
                sort_by=request.sort_by,
                group_by=request.group_by,
                aggregations=request.aggregations,
                matches=request.matches,
            )
            return MetaToolOutput(action="query", data=result)

        case _:
            raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# bamboo_reports (4 actions)
# =============================================================================
async def bamboo_reports(request: ReportsInput) -> MetaToolOutput:
    """Run company and custom reports."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=REPORTS_HELP)

        case "company":
            if not request.report_id:
                raise ValueError("reportId is required for company action")
            result = await run_company_report(request.report_id)
            return _make_output("company", result)

        case "custom_list":
            _check_role("hr_admin")  # custom_list requires hr_admin role
            result = await get_custom_reports()
            return _make_output("custom_list", result)

        case "custom_get":
            _check_role("hr_admin")  # custom_get requires hr_admin role
            if not request.report_id:
                raise ValueError("reportId is required for custom_get action")
            result = await get_custom_report(request.report_id)
            return _make_output("custom_get", result)

        case "custom_run":
            _check_role("hr_admin")  # custom_run requires hr_admin role
            if not request.title:
                raise ValueError("title is required for custom_run action")
            result = await run_custom_report(
                title=request.title,
                fields=request.fields,
                _filters=None,
            )
            return _make_output("custom_run", result)

        case _:
            raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# bamboo_employees (5 actions)
# =============================================================================
async def bamboo_employees(request: EmployeesInput) -> MetaToolOutput:
    """Manage employee records: get, create, update, directory, company info."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=EMPLOYEES_HELP)

        case "get":
            if not request.employee_id:
                raise ValueError("employeeId is required for get action")
            result = await get_employee(request.employee_id, request.fields)
            return MetaToolOutput(action="get", data=result)

        case "create":
            _check_role("hr_admin")  # create requires hr_admin role
            _check_scope("write:employees")  # create requires write:employees scope
            if not request.first_name:
                raise ValueError("firstName is required for create action")
            if not request.last_name:
                raise ValueError("lastName is required for create action")

            input_data = CreateEmployeeInput(
                firstName=request.first_name,
                lastName=request.last_name,
                preferredName=request.preferred_name,
                middleName=request.middle_name,
                workEmail=request.work_email,
                employeeNumber=request.employee_number,
                hireDate=request.hire_date,
                department=request.department,
                jobTitle=request.job_title,
                location=request.location,
                supervisorId=request.supervisor_id,
                status=request.status,
                workPhone=request.work_phone,
                mobilePhone=request.mobile_phone,
                address1=request.address1,
                address2=request.address2,
                city=request.city,
                state=request.state,
                zipcode=request.zipcode,
                country=request.country,
                ssn=request.ssn,
                dateOfBirth=request.date_of_birth,
                gender=request.gender,
                maritalStatus=request.marital_status,
                ethnicity=request.ethnicity,
                salary=request.salary,
                payType=request.pay_type,
                payRate=request.pay_rate,
                payPer=request.pay_per,
                paySchedule=request.pay_schedule,
                linkedIn=request.linked_in,
                division=request.division,
                workPhoneExtension=request.work_phone_extension,
                homeEmail=request.home_email,
                photoUrl=request.photo_url,
                displayName=request.display_name,
                terminationDate=request.termination_date,
            )
            result = await create_employee(input_data)
            return MetaToolOutput(action="create", data=result.model_dump(by_alias=True))

        case "update":
            # update requires hr_admin or manager role (manager has field restrictions)
            # user_has_role returns True if auth disabled, granting full access
            if not user_has_role("hr_admin") and not user_has_role("manager"):
                raise PermissionError("Access denied: Requires 'hr_admin' or 'manager' role")
            if not request.employee_id:
                raise ValueError("employeeId is required for update action")
            result = await update_employee(
                employeeId=request.employee_id,
                firstName=request.first_name,
                lastName=request.last_name,
                preferredName=request.preferred_name,
                middleName=request.middle_name,
                workEmail=request.work_email,
                employeeNumber=request.employee_number,
                department=request.department,
                jobTitle=request.job_title,
                location=request.location,
                supervisorId=request.supervisor_id,
                status=request.status,
                workPhone=request.work_phone,
                mobilePhone=request.mobile_phone,
                address1=request.address1,
                address2=request.address2,
                city=request.city,
                state=request.state,
                zipcode=request.zipcode,
                country=request.country,
                ssn=request.ssn,
                dateOfBirth=request.date_of_birth,
                gender=request.gender,
                maritalStatus=request.marital_status,
                ethnicity=request.ethnicity,
                salary=request.salary,
                payType=request.pay_type,
                payRate=request.pay_rate,
                payPer=request.pay_per,
                paySchedule=request.pay_schedule,
                linkedIn=request.linked_in,
                division=request.division,
                workPhoneExtension=request.work_phone_extension,
                homeEmail=request.home_email,
                photoUrl=request.photo_url,
                displayName=request.display_name,
                terminationDate=request.termination_date,
                hireDate=request.hire_date,
            )
            return MetaToolOutput(action="update", data=result)

        case "directory":
            result = await get_directory_for_persona()
            return MetaToolOutput(action="directory", data=result.model_dump(by_alias=True))

        case "company_info":
            result = await get_company_info()
            return MetaToolOutput(action="company_info", data=result)

        case _:
            raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# bamboo_time_off (11 actions)
# =============================================================================
async def bamboo_time_off(request: TimeOffInput) -> MetaToolOutput:
    """Manage time-off: requests, balances, policies, and who's out."""
    match request.action:
        case "help":
            return MetaToolOutput(action="help", help=TIME_OFF_HELP)

        case "types":
            result = await get_types(mode=request.mode)
            return MetaToolOutput(action="types", data=result)

        case "requests_list":
            if not request.start:
                raise ValueError("start is required for requests_list action")
            if not request.end:
                raise ValueError("end is required for requests_list action")
            result = await get_requests(
                start=request.start,
                end=request.end,
                employee_id=request.employee_id,
                request_id=request.request_id,
                status=[request.status] if request.status else None,
                type_id=request.time_off_type_id,
            )
            return MetaToolOutput(action="requests_list", data=result)

        case "request_create":
            if not request.employee_id:
                raise ValueError("employeeId is required for request_create action")
            if not request.time_off_type_id:
                raise ValueError("timeOffTypeId is required for request_create action")
            if not request.start:
                raise ValueError("start is required for request_create action")
            if not request.end:
                raise ValueError("end is required for request_create action")
            if not request.amount:
                raise ValueError("amount is required for request_create action")
            result = await create_request(
                employee_id=request.employee_id,
                time_off_type_id=request.time_off_type_id,
                start=request.start,
                end=request.end,
                amount=request.amount,
                notes=request.notes,
                status=request.status or "requested",
            )
            return MetaToolOutput(action="request_create", data=result)

        case "request_update_status":
            if not request.request_id:
                raise ValueError("requestId is required for request_update_status action")
            if not request.status:
                raise ValueError("status is required for request_update_status action")
            # Import UpdateRequestStatusInput to build proper request
            from .time_off_requests import UpdateRequestStatusInput

            input_data = UpdateRequestStatusInput(
                requestId=request.request_id,
                status=request.status,
                note=request.note,
            )
            result = await update_request_status(input_data)
            return MetaToolOutput(
                action="request_update_status", data=result.model_dump(by_alias=True)
            )

        case "balances_get":
            if not request.employee_id:
                raise ValueError("employeeId is required for balances_get action")
            result = await get_balances(employeeId=request.employee_id)
            return MetaToolOutput(action="balances_get", data=result)

        case "balance_update":
            _check_role("hr_admin")  # balance_update requires hr_admin role
            if not request.employee_id:
                raise ValueError("employeeId is required for balance_update action")
            if not request.time_off_type_id:
                raise ValueError("timeOffTypeId is required for balance_update action")
            if not request.amount:
                raise ValueError("amount is required for balance_update action")
            if not request.note:
                raise ValueError("note is required for balance_update action")
            # Validate numeric conversions
            try:
                type_id = int(request.time_off_type_id)
            except ValueError:
                raise ValueError(
                    f"timeOffTypeId must be a valid integer, got: {request.time_off_type_id}"
                )
            try:
                amount_val = float(request.amount)
            except ValueError:
                raise ValueError(f"amount must be a valid number, got: {request.amount}")
            result = await update_balance(
                employeeId=request.employee_id,
                timeOffTypeId=type_id,
                amount=amount_val,
                note=request.note,
                date=request.date,
            )
            return MetaToolOutput(action="balance_update", data=result)

        case "policies_list":
            result = await get_policies()
            return MetaToolOutput(action="policies_list", data=result)

        case "policies_get":
            if not request.employee_id:
                raise ValueError("employeeId is required for policies_get action")
            result = await get_employee_policies(employeeId=request.employee_id)
            return MetaToolOutput(action="policies_get", data=result)

        case "policy_assign":
            _check_role("hr_admin")  # policy_assign requires hr_admin role
            if not request.employee_id:
                raise ValueError("employeeId is required for policy_assign action")
            if not request.policies:
                raise ValueError("policies is required for policy_assign action")
            result = await assign_policy(
                employeeId=request.employee_id,
                policies=request.policies,
            )
            return MetaToolOutput(action="policy_assign", data=result)

        case "whos_out":
            if not request.start:
                raise ValueError("start is required for whos_out action")
            if not request.end:
                raise ValueError("end is required for whos_out action")
            result = await get_whos_out(start=request.start, end=request.end)
            return MetaToolOutput(action="whos_out", data=result)

        case "estimate":
            if not request.employee_id:
                raise ValueError("employeeId is required for estimate action")
            if not request.date:
                raise ValueError("date is required for estimate action")
            result = await estimate_future_balances(
                employeeId=request.employee_id,
                date=request.date,
            )
            return MetaToolOutput(action="estimate", data=result)

        case _:
            raise ValueError(f"Unknown action: {request.action}")


# =============================================================================
# bamboo_schema (utility)
# =============================================================================
# Tool schemas for introspection
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "bamboo_admin": {"input": AdminInput, "output": MetaToolOutput},
    "bamboo_metadata": {"input": MetadataInput, "output": MetaToolOutput},
    "bamboo_search": {"input": SearchInput, "output": MetaToolOutput},
    "bamboo_datasets": {"input": DatasetsInput, "output": MetaToolOutput},
    "bamboo_reports": {"input": ReportsInput, "output": MetaToolOutput},
    "bamboo_employees": {"input": EmployeesInput, "output": MetaToolOutput},
    "bamboo_time_off": {"input": TimeOffInput, "output": MetaToolOutput},
    "bamboo_schema": {"input": SchemaInput, "output": SchemaOutput},
}


async def bamboo_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for any meta-tool's input and output."""
    if request.tool_name not in TOOL_SCHEMAS:
        available = list(TOOL_SCHEMAS.keys())
        raise ValueError(f"Unknown tool: {request.tool_name}. Available: {available}")

    schemas = TOOL_SCHEMAS[request.tool_name]
    input_schema = schemas["input"].model_json_schema()
    output_schema = schemas["output"].model_json_schema() if schemas["output"] else None

    return SchemaOutput(
        tool=request.tool_name,
        input_schema=input_schema,
        output_schema=output_schema,
    )


# =============================================================================
# Exports
# =============================================================================
__all__ = [
    "bamboo_admin",
    "bamboo_metadata",
    "bamboo_search",
    "bamboo_datasets",
    "bamboo_reports",
    "bamboo_employees",
    "bamboo_time_off",
    "bamboo_schema",
]
