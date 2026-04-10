"""Meta-tools schemas for BambooHR MCP server.

This module defines Pydantic input/output models for the 8 domain meta-tools
that consolidate 36 individual tools into action-parameter routing for LLMs.

Meta-tools provide ~78% context reduction for LLM agents while preserving
individual tools for UI forms.

Domains:
- bamboo_admin: Server info, reset state (2 actions)
- bamboo_metadata: Countries, states, fields, users (7 actions)
- bamboo_search: Employee, time-off, metadata search (3 actions)
- bamboo_datasets: Dataset listing and querying (4 actions)
- bamboo_reports: Company and custom reports (4 actions)
- bamboo_employees: Employee CRUD (5 actions)
- bamboo_time_off: Requests, balances, policies (11 actions)
- bamboo_schema: Tool schema introspection
"""

from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field


# =============================================================================
# Base Models
# =============================================================================
class HelpResponse(BaseModel):
    """Standard help response for meta-tools."""

    tool_name: str = Field(..., description="Name of the meta-tool")
    description: str = Field(..., description="Description of the meta-tool")
    actions: dict[str, dict[str, Any]] = Field(
        ..., description="Available actions with their parameters"
    )


class MetaToolOutput(BaseModel):
    """Generic output for meta-tools."""

    model_config = ConfigDict(populate_by_name=True)

    action: str = Field(..., description="Action that was executed")
    help: "HelpResponse | None" = Field(None, description="Help response (for action='help')")
    data: dict[str, Any] | list[Any] | None = Field(None, description="Action result data")
    error: str | None = Field(None, description="Error message if action failed")


# =============================================================================
# bamboo_admin (2 actions)
# =============================================================================
class AdminInput(BaseModel):
    """Input for bamboo_admin meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "server_info", "reset_state"] = Field(
        ..., description="Action: 'help', 'server_info', 'reset_state'"
    )
    # reset_state params
    confirm: bool | None = Field(
        None, description="Must be true to confirm database reset (required for reset_state)"
    )


ADMIN_HELP = HelpResponse(
    tool_name="bamboo_admin",
    description="Administrative operations for BambooHR server.",
    actions={
        "help": {"description": "Show available actions", "required_params": []},
        "server_info": {
            "description": "Get server status and feature availability",
            "required_params": [],
        },
        "reset_state": {
            "description": "Reset database to empty state (HR Admin only)",
            "required_params": ["confirm"],
            "optional_params": [],
        },
    },
)


# =============================================================================
# bamboo_metadata (7 actions)
# =============================================================================
class MetadataInput(BaseModel):
    """Input for bamboo_metadata meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal[
        "help",
        "countries",
        "states",
        "fields",
        "list_fields",
        "field_options",
        "update_options",
        "users",
    ] = Field(
        ...,
        description="Action: 'help', 'countries', 'states', 'fields', 'list_fields', "
        "'field_options', 'update_options', 'users'",
    )
    # states params
    country_code: str | None = Field(
        None, alias="countryCode", description="ISO country code (required for states)"
    )
    # field_options/update_options params
    field_id: str | None = Field(
        None, alias="fieldId", description="Field ID or alias (required for field_options)"
    )
    list_field_id: str | None = Field(
        None, alias="listFieldId", description="List field ID (required for update_options)"
    )
    options: list[dict[str, Any]] | None = Field(
        None, description="Options to create/update (required for update_options)"
    )


METADATA_HELP = HelpResponse(
    tool_name="bamboo_metadata",
    description="Access system metadata: countries, states, fields, and users.",
    actions={
        "help": {"description": "Show available actions", "required_params": []},
        "countries": {"description": "List all supported countries", "required_params": []},
        "states": {
            "description": "List states/provinces for a country",
            "required_params": ["countryCode"],
        },
        "fields": {"description": "Get all field definitions", "required_params": []},
        "list_fields": {
            "description": "Get list-type fields with options",
            "required_params": [],
        },
        "field_options": {
            "description": "Get options for a specific field",
            "required_params": ["fieldId"],
        },
        "update_options": {
            "description": "Update field options (HR Admin only)",
            "required_params": ["listFieldId", "options"],
        },
        "users": {
            "description": "List BambooHR users (HR Admin/Manager only)",
            "required_params": [],
        },
    },
)


# =============================================================================
# bamboo_search (3 actions)
# =============================================================================
class SearchInput(BaseModel):
    """Input for bamboo_search meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "employees", "time_off", "metadata"] = Field(
        ..., description="Action: 'help', 'employees', 'time_off', 'metadata'"
    )
    # Common search params
    query: str | None = Field(None, description="Search query string")
    fields: list[str] | None = Field(None, description="Fields to search within")
    filters: dict[str, Any] | None = Field(None, description="Filter conditions")
    page: int = Field(
        1, ge=1, description="Page number (1-indexed). Use with page_size for pagination."
    )
    page_size: int = Field(
        20,
        ge=1,
        le=100,
        alias="pageSize",
        description="Results per page (1-100). Default: 20. Use with page for pagination.",
    )
    # metadata-specific params
    entity_types: list[str] | None = Field(
        None, alias="entityTypes", description="Entity types to search (for metadata)"
    )


SEARCH_HELP = HelpResponse(
    tool_name="bamboo_search",
    description="Search across employees, time-off, and metadata with fuzzy matching.",
    actions={
        "help": {"description": "Show available actions", "required_params": []},
        "employees": {
            "description": "Search employees by name, email, department, etc.",
            "required_params": [],
            "optional_params": ["query", "fields", "filters", "page", "pageSize"],
        },
        "time_off": {
            "description": "Search time-off requests",
            "required_params": [],
            "optional_params": ["query", "fields", "filters", "page", "pageSize"],
        },
        "metadata": {
            "description": "Search metadata entities",
            "required_params": [],
            "optional_params": ["query", "entityTypes", "page", "pageSize"],
        },
    },
)


# =============================================================================
# bamboo_datasets (4 actions)
# =============================================================================
class DatasetsInput(BaseModel):
    """Input for bamboo_datasets meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "list", "fields", "field_options", "query"] = Field(
        ..., description="Action: 'help', 'list', 'fields', 'field_options', 'query'"
    )
    # Dataset params
    dataset_name: str | None = Field(
        None, alias="datasetName", description="Dataset name (e.g., 'employees', 'timeOff')"
    )
    field_id: str | None = Field(None, alias="fieldId", description="Field ID for field_options")
    # Query params
    fields: list[str] | None = Field(None, description="Fields to return (required for query)")
    filters: list[dict[str, Any]] | None = Field(None, description="Filter conditions for query")
    sort_by: list[dict[str, str]] | None = Field(
        None, alias="sortBy", description="Sort specifications [{field, sort}]"
    )
    group_by: list[str] | None = Field(None, alias="groupBy", description="Fields to group by")
    aggregations: list[dict[str, str]] | None = Field(
        None, description="Aggregations [{field, function}]"
    )
    matches: Literal["all", "any"] = Field(
        "all", description="Filter matching: 'all' (AND) or 'any' (OR)"
    )


DATASETS_HELP = HelpResponse(
    tool_name="bamboo_datasets",
    description="Query BambooHR datasets with filtering, sorting, and aggregations.",
    actions={
        "help": {"description": "Show available actions", "required_params": []},
        "list": {"description": "List all available datasets", "required_params": []},
        "fields": {
            "description": "Get field definitions for a dataset",
            "required_params": ["datasetName"],
        },
        "field_options": {
            "description": "Get options for a dataset field",
            "required_params": ["datasetName", "fieldId"],
        },
        "query": {
            "description": "Execute dataset query (HR Admin only)",
            "required_params": ["datasetName", "fields"],
            "optional_params": ["filters", "sortBy", "groupBy", "aggregations", "matches"],
        },
    },
)


# =============================================================================
# bamboo_reports (4 actions)
# =============================================================================
class ReportsInput(BaseModel):
    """Input for bamboo_reports meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "company", "custom_list", "custom_get", "custom_run"] = Field(
        ..., description="Action: 'help', 'company', 'custom_list', 'custom_get', 'custom_run'"
    )
    # Report params
    report_id: str | None = Field(
        None, alias="reportId", description="Report ID (required for company, custom_get)"
    )
    # custom_run params
    title: str | None = Field(None, description="Report title (required for custom_run)")
    fields: list[str] | None = Field(None, description="Fields to include (for custom_run)")


REPORTS_HELP = HelpResponse(
    tool_name="bamboo_reports",
    description="Run company and custom reports.",
    actions={
        "help": {"description": "Show available actions", "required_params": []},
        "company": {
            "description": "Run a company report by ID",
            "required_params": ["reportId"],
        },
        "custom_list": {
            "description": "List custom reports (HR Admin only)",
            "required_params": [],
        },
        "custom_get": {
            "description": "Get custom report metadata (HR Admin only)",
            "required_params": ["reportId"],
        },
        "custom_run": {
            "description": "Run a custom report (HR Admin only)",
            "required_params": ["title"],
            "optional_params": ["fields"],
        },
    },
)


# =============================================================================
# bamboo_employees (5 actions)
# =============================================================================
class EmployeesInput(BaseModel):
    """Input for bamboo_employees meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal["help", "get", "create", "update", "directory", "company_info"] = Field(
        ..., description="Action: 'help', 'get', 'create', 'update', 'directory', 'company_info'"
    )
    # Common params
    employee_id: str | None = Field(
        None, alias="employeeId", description="Employee ID (required for get/update)"
    )
    fields: str | None = Field(None, description="Comma-separated field list (for get)")

    # Create/update employee fields
    first_name: str | None = Field(
        None, alias="firstName", description="First name. REQUIRED for create action."
    )
    last_name: str | None = Field(
        None, alias="lastName", description="Last name. REQUIRED for create action."
    )
    preferred_name: str | None = Field(None, alias="preferredName", description="Preferred name")
    middle_name: str | None = Field(None, alias="middleName", description="Middle name")
    work_email: str | None = Field(None, alias="workEmail", description="Work email address")
    employee_number: str | None = Field(None, alias="employeeNumber", description="Employee number")
    hire_date: str | None = Field(
        None,
        alias="hireDate",
        description="Hire date (YYYY-MM-DD). REQUIRED for create. Example: 2024-01-15.",
    )
    department: str | None = Field(None, description="Department name")
    job_title: str | None = Field(None, alias="jobTitle", description="Job title")
    location: str | None = Field(None, description="Work location")
    supervisor_id: int | None = Field(None, alias="supervisorId", description="Supervisor's ID")
    status: Literal["Active", "Inactive", "Terminated"] | None = Field(
        None, description="Employment status"
    )
    work_phone: str | None = Field(None, alias="workPhone", description="Work phone")
    mobile_phone: str | None = Field(None, alias="mobilePhone", description="Mobile phone")
    address1: str | None = Field(None, description="Address line 1")
    address2: str | None = Field(None, description="Address line 2")
    city: str | None = Field(None, description="City")
    state: str | None = Field(None, description="State/Province")
    zipcode: str | None = Field(None, description="ZIP/Postal code")
    country: str | None = Field(None, description="Country")
    ssn: str | None = Field(None, description="Social Security Number")
    date_of_birth: str | None = Field(
        None, alias="dateOfBirth", description="Date of birth (YYYY-MM-DD)"
    )
    gender: str | None = Field(None, description="Gender")
    marital_status: str | None = Field(None, alias="maritalStatus", description="Marital status")
    ethnicity: str | None = Field(None, description="Ethnicity")
    salary: str | None = Field(None, description="Salary amount")
    pay_type: str | None = Field(None, alias="payType", description="Pay type")
    pay_rate: str | None = Field(None, alias="payRate", description="Pay rate")
    pay_per: str | None = Field(None, alias="payPer", description="Pay period")
    pay_schedule: str | None = Field(None, alias="paySchedule", description="Pay schedule")
    linked_in: str | None = Field(None, alias="linkedIn", description="LinkedIn URL")
    division: str | None = Field(None, description="Division")
    work_phone_extension: str | None = Field(
        None, alias="workPhoneExtension", description="Work phone extension"
    )
    home_email: str | None = Field(None, alias="homeEmail", description="Home email")
    photo_url: str | None = Field(None, alias="photoUrl", description="Photo URL")
    display_name: str | None = Field(None, alias="displayName", description="Display name")
    termination_date: str | None = Field(
        None, alias="terminationDate", description="Termination date (YYYY-MM-DD)"
    )


EMPLOYEES_HELP = HelpResponse(
    tool_name="bamboo_employees",
    description="Manage employee records: get, create, update, directory, company info.",
    actions={
        "help": {"description": "Show available actions", "required_params": []},
        "get": {
            "description": "Get employee by ID",
            "required_params": ["employeeId"],
            "optional_params": ["fields"],
        },
        "create": {
            "description": "Create new employee (HR Admin only)",
            "required_params": ["firstName", "lastName"],
            "optional_params": [
                "workEmail",
                "department",
                "jobTitle",
                "hireDate",
                "supervisorId",
                "...",
            ],
        },
        "update": {
            "description": "Update employee fields",
            "required_params": ["employeeId"],
            "optional_params": ["firstName", "lastName", "department", "..."],
        },
        "directory": {
            "description": "Get employee directory (filtered by persona)",
            "required_params": [],
        },
        "company_info": {
            "description": "Get company information and directory fields",
            "required_params": [],
        },
    },
)


# =============================================================================
# bamboo_time_off (11 actions)
# =============================================================================
class TimeOffInput(BaseModel):
    """Input for bamboo_time_off meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    action: Literal[
        "help",
        "types",
        "requests_list",
        "request_create",
        "request_update_status",
        "balances_get",
        "balance_update",
        "policies_list",
        "policies_get",
        "policy_assign",
        "whos_out",
        "estimate",
    ] = Field(
        ...,
        description="Action: 'help', 'types', 'requests_list', 'request_create', "
        "'request_update_status', 'balances_get', 'balance_update', 'policies_list', "
        "'policies_get', 'policy_assign', 'whos_out', 'estimate'",
    )

    # Common params
    employee_id: str | None = Field(None, alias="employeeId", description="Employee ID")

    # Date range params (requests_list, whos_out)
    start: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    end: str | None = Field(None, description="End date (YYYY-MM-DD)")

    # Request params
    request_id: str | None = Field(None, alias="requestId", description="Request ID")
    time_off_type_id: str | None = Field(
        None, alias="timeOffTypeId", description="Time-off type ID"
    )
    amount: str | None = Field(None, description="Amount of time off")
    notes: str | None = Field(None, description="Request notes")
    status: Literal["requested", "approved", "denied", "canceled", "superseded"] | None = Field(
        None, description="Request status: requested, approved, denied, canceled, or superseded"
    )
    note: str | None = Field(None, description="Status update note")

    # Balance update params
    date: str | None = Field(None, description="Adjustment date (YYYY-MM-DD)")

    # Policy assignment params
    policies: list[dict[str, Any]] | None = Field(
        None, description="Policies to assign [{timeOffPolicyId, accrualStartDate}]"
    )

    # Types filter
    mode: str | None = Field(None, description="Filter mode for types (e.g., 'request')")


TIME_OFF_HELP = HelpResponse(
    tool_name="bamboo_time_off",
    description="Manage time-off: requests, balances, policies, and who's out.",
    actions={
        "help": {"description": "Show available actions", "required_params": []},
        "types": {
            "description": "Get time-off types",
            "required_params": [],
            "optional_params": ["mode"],
        },
        "requests_list": {
            "description": "List time-off requests",
            "required_params": ["start", "end"],
            "optional_params": ["employeeId", "requestId", "status", "timeOffTypeId"],
        },
        "request_create": {
            "description": "Create a time-off request",
            "required_params": ["employeeId", "timeOffTypeId", "start", "end", "amount"],
            "optional_params": ["notes", "status"],
        },
        "request_update_status": {
            "description": "Update request status (approve/deny/cancel)",
            "required_params": ["requestId", "status"],
            "optional_params": ["note"],
        },
        "balances_get": {
            "description": "Get time-off balances for employee",
            "required_params": ["employeeId"],
        },
        "balance_update": {
            "description": "Adjust time-off balance (HR Admin only)",
            "required_params": ["employeeId", "timeOffTypeId", "amount", "note"],
            "optional_params": ["date"],
        },
        "policies_list": {
            "description": "List all time-off policies",
            "required_params": [],
        },
        "policies_get": {
            "description": "Get policies for an employee",
            "required_params": ["employeeId"],
        },
        "policy_assign": {
            "description": "Assign policies to employee (HR Admin only)",
            "required_params": ["employeeId", "policies"],
        },
        "whos_out": {
            "description": "Get who's out in date range",
            "required_params": ["start", "end"],
        },
        "estimate": {
            "description": "Estimate future balances",
            "required_params": ["employeeId", "date"],
        },
    },
)


# =============================================================================
# bamboo_schema (utility)
# =============================================================================
class SchemaInput(BaseModel):
    """Input for bamboo_schema meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    tool_name: str = Field(..., alias="toolName", description="Name of tool to get schema for")


class SchemaOutput(BaseModel):
    """Output for bamboo_schema meta-tool."""

    model_config = ConfigDict(populate_by_name=True)

    tool: str = Field(..., description="Tool name")
    input_schema: dict[str, Any] = Field(..., alias="inputSchema", description="JSON schema")
    output_schema: dict[str, Any] | None = Field(
        None, alias="outputSchema", description="Output schema if available"
    )


# =============================================================================
# Exports
# =============================================================================
__all__ = [
    # Base
    "HelpResponse",
    "MetaToolOutput",
    # Admin
    "AdminInput",
    "ADMIN_HELP",
    # Metadata
    "MetadataInput",
    "METADATA_HELP",
    # Search
    "SearchInput",
    "SEARCH_HELP",
    # Datasets
    "DatasetsInput",
    "DATASETS_HELP",
    # Reports
    "ReportsInput",
    "REPORTS_HELP",
    # Employees
    "EmployeesInput",
    "EMPLOYEES_HELP",
    # Time Off
    "TimeOffInput",
    "TIME_OFF_HELP",
    # Schema
    "SchemaInput",
    "SchemaOutput",
]
