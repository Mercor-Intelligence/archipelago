"""Report tools for BambooHR MCP server.

Implements:
- bamboo.reports.run_company_report: Run standard company report
- bamboo.reports.get_custom_reports: List all custom reports
- bamboo.reports.get_custom_report: Get specific custom report metadata
- bamboo.reports.run_custom_report: Run custom report with field selection

Per BUILD_PLAN sections 3.2.24-3.2.27:
- Company reports: All personas can run, results filtered by persona
- Custom reports: HR Admin only (deprecated but supported)
- Requires read:employees scope
"""

from typing import Any

from db import Employee, get_session
from mcp_auth import require_scopes
from sqlalchemy import select

from .auth_helpers import get_user_context

# Standard company reports
# Per BUILD_PLAN: Common reportId values
COMPANY_REPORTS: dict[str, dict[str, Any]] = {
    "1": {
        "title": "Employee Directory",
        "fields": [
            {"id": "firstName", "name": "First Name", "type": "text"},
            {"id": "lastName", "name": "Last Name", "type": "text"},
            {"id": "workEmail", "name": "Work Email", "type": "email"},
            {"id": "department", "name": "Department", "type": "list"},
            {"id": "jobTitle", "name": "Job Title", "type": "list"},
            {"id": "status", "name": "Status", "type": "text"},
        ],
    },
    "2": {
        "title": "Time-Off Summary",
        "fields": [
            {"id": "firstName", "name": "First Name", "type": "text"},
            {"id": "lastName", "name": "Last Name", "type": "text"},
            {"id": "department", "name": "Department", "type": "list"},
        ],
    },
    "3": {
        "title": "Benefits Enrollment",
        "fields": [
            {"id": "firstName", "name": "First Name", "type": "text"},
            {"id": "lastName", "name": "Last Name", "type": "text"},
            {"id": "hireDate", "name": "Hire Date", "type": "date"},
        ],
    },
    "4": {
        "title": "Performance Reviews",
        "fields": [
            {"id": "firstName", "name": "First Name", "type": "text"},
            {"id": "lastName", "name": "Last Name", "type": "text"},
            {"id": "department", "name": "Department", "type": "list"},
            {"id": "supervisorId", "name": "Supervisor", "type": "employee"},
        ],
    },
}

# Custom reports (saved report definitions)
# Per BUILD_PLAN: HR Admin only, deprecated but supported
CUSTOM_REPORTS: dict[str, dict[str, Any]] = {
    "100": {
        "id": "100",
        "title": "Quarterly Headcount",
        "fields": ["firstName", "lastName", "department", "hireDate"],
    },
    "101": {
        "id": "101",
        "title": "Department Breakdown",
        "fields": ["firstName", "lastName", "department", "jobTitle"],
    },
    "102": {
        "id": "102",
        "title": "New Hires Report",
        "fields": ["firstName", "lastName", "hireDate", "department", "supervisorId"],
    },
}

# Valid field definitions for custom reports
# Maps field ID to field metadata
# Must match fields exposed by meta.get_fields (FIELD_DEFINITIONS in meta.py)
VALID_FIELDS: dict[str, dict[str, str]] = {
    # Name fields
    "firstName": {"id": "firstName", "name": "First name", "type": "text"},
    "lastName": {"id": "lastName", "name": "Last name", "type": "text"},
    "preferredName": {"id": "preferredName", "name": "Preferred name", "type": "text"},
    "middleName": {"id": "middleName", "name": "Middle name", "type": "text"},
    "displayName": {"id": "displayName", "name": "Display name", "type": "text"},
    "employeeNumber": {"id": "employeeNumber", "name": "Employee number", "type": "text"},
    # Contact fields
    "workEmail": {"id": "workEmail", "name": "Work email", "type": "email"},
    "homeEmail": {"id": "homeEmail", "name": "Home email", "type": "email"},
    "workPhone": {"id": "workPhone", "name": "Work phone", "type": "phone"},
    "workPhoneExtension": {
        "id": "workPhoneExtension",
        "name": "Work phone extension",
        "type": "text",
    },
    "mobilePhone": {"id": "mobilePhone", "name": "Mobile phone", "type": "phone"},
    # Date fields
    "hireDate": {"id": "hireDate", "name": "Hire Date", "type": "date"},
    "terminationDate": {"id": "terminationDate", "name": "Termination date", "type": "date"},
    "dateOfBirth": {"id": "dateOfBirth", "name": "Date of birth", "type": "date"},
    # Sensitive fields (HR Admin only via custom reports)
    "ssn": {"id": "ssn", "name": "SSN", "type": "ssn"},
    "gender": {"id": "gender", "name": "Gender", "type": "gender"},
    # List fields
    "department": {"id": "department", "name": "Department", "type": "list"},
    "division": {"id": "division", "name": "Division", "type": "list"},
    "location": {"id": "location", "name": "Location", "type": "list"},
    "status": {"id": "status", "name": "Employment Status", "type": "list"},
    "jobTitle": {"id": "jobTitle", "name": "Job Title", "type": "list"},
    # Employee reference
    "supervisorId": {"id": "supervisorId", "name": "Supervisor", "type": "employee"},
    # Photo
    "photoUrl": {"id": "photoUrl", "name": "Photo", "type": "photo"},
    # Address fields
    "address1": {"id": "address1", "name": "Address line 1", "type": "text"},
    "address2": {"id": "address2", "name": "Address line 2", "type": "text"},
    "city": {"id": "city", "name": "City", "type": "text"},
    "state": {"id": "state", "name": "State", "type": "state"},
    "zipcode": {"id": "zipcode", "name": "Zip code", "type": "text"},
    "country": {"id": "country", "name": "Country", "type": "country"},
    # Social
    "linkedIn": {"id": "linkedIn", "name": "LinkedIn", "type": "text"},
    # Personal (HR Admin only)
    "maritalStatus": {"id": "maritalStatus", "name": "Marital status", "type": "maritalStatus"},
    "ethnicity": {"id": "ethnicity", "name": "Ethnicity", "type": "text"},
    # Compensation (HR Admin only)
    "salary": {"id": "salary", "name": "Salary", "type": "currency"},
    "payRate": {"id": "payRate", "name": "Pay rate", "type": "currency"},
    "payPer": {"id": "payPer", "name": "Pay per", "type": "text"},
    "payType": {"id": "payType", "name": "Pay type", "type": "payType"},
    "paySchedule": {"id": "paySchedule", "name": "Pay schedule", "type": "text"},
}

# Default fields for custom reports when none specified
DEFAULT_CUSTOM_REPORT_FIELDS = ["firstName", "lastName", "department", "jobTitle", "status"]


async def _get_direct_report_ids(session: Any, manager_id: int) -> set[int]:
    """Get IDs of all direct reports for a manager.

    Args:
        session: Active database session
        manager_id: ID of the manager

    Returns:
        Set of employee IDs that report to this manager
    """
    result = await session.execute(select(Employee.id).where(Employee.supervisor_id == manager_id))
    return {row[0] for row in result.fetchall()}


def _employee_to_report_row(employee: Employee) -> dict[str, Any]:
    """Convert employee model to report row format.

    Args:
        employee: Employee model instance

    Returns:
        Dictionary with camelCase field names for report output
    """
    row: dict[str, Any] = {
        "id": str(employee.id),
        "firstName": employee.first_name,
        "lastName": employee.last_name,
    }

    # Add optional fields - include null fields per BUILD_PLAN section 2
    row["workEmail"] = employee.work_email
    row["department"] = employee.department
    row["jobTitle"] = employee.job_title
    row["status"] = employee.status
    row["hireDate"] = employee.hire_date.isoformat() if employee.hire_date else None
    row["supervisorId"] = (
        str(employee.supervisor_id) if employee.supervisor_id is not None else None
    )

    return row


@require_scopes("read:employees")
async def run_company_report(report_id: str) -> dict[str, Any]:
    """Run a standard company report."""
    # Validate report ID
    if report_id not in COMPANY_REPORTS:
        return {
            "error": {
                "code": 404,
                "message": f"Report {report_id} not found",
            }
        }

    report_config = COMPANY_REPORTS[report_id]

    # Get user context for filtering
    user_employee_id, persona = get_user_context()

    # Query employees based on persona
    async with get_session() as session:
        if persona == "hr_admin":
            # HR Admin sees all employees
            result = await session.execute(
                select(Employee).order_by(Employee.last_name, Employee.first_name)
            )
            employees = list(result.scalars().all())
        elif persona == "manager":
            # Manager sees self + direct reports
            if user_employee_id is not None:
                direct_report_ids = await _get_direct_report_ids(session, user_employee_id)
                allowed_ids = direct_report_ids | {user_employee_id}
                result = await session.execute(
                    select(Employee)
                    .where(Employee.id.in_(allowed_ids))
                    .order_by(Employee.last_name, Employee.first_name)
                )
                employees = list(result.scalars().all())
            else:
                # Manager without employeeId in token sees no employees
                employees = []
        else:
            # Employee sees only self
            if user_employee_id is not None:
                result = await session.execute(
                    select(Employee).where(Employee.id == user_employee_id)
                )
                employees = list(result.scalars().all())
            else:
                employees = []

    # Convert employees to report format
    employee_rows = [_employee_to_report_row(emp) for emp in employees]

    return {
        "title": report_config["title"],
        "fields": list(report_config["fields"]),
        "employees": employee_rows,
    }


@require_scopes("read:employees")
async def get_custom_reports() -> list[dict[str, str]] | dict[str, Any]:
    """List all custom reports."""
    # Get user context and check HR Admin
    _, persona = get_user_context()

    if persona != "hr_admin":
        return {
            "error": {
                "code": 403,
                "message": "Only HR Admin can access custom reports",
            }
        }

    # Return list of custom reports
    return [{"id": report["id"], "title": report["title"]} for report in CUSTOM_REPORTS.values()]


@require_scopes("read:employees")
async def get_custom_report(report_id: str) -> dict[str, Any]:
    """Get metadata for a specific custom report."""
    # Get user context and check HR Admin
    _, persona = get_user_context()

    if persona != "hr_admin":
        return {
            "error": {
                "code": 403,
                "message": "Only HR Admin can access custom reports",
            }
        }

    # Look up custom report
    if report_id not in CUSTOM_REPORTS:
        return {
            "error": {
                "code": 404,
                "message": f"Custom report {report_id} not found",
            }
        }

    report = CUSTOM_REPORTS[report_id]
    return {
        "id": report["id"],
        "title": report["title"],
        "fields": list(report["fields"]),
    }


def _employee_to_custom_report_row(employee: Employee, fields: list[str]) -> dict[str, Any]:
    """Convert employee model to custom report row with selected fields.

    Args:
        employee: Employee model instance
        fields: List of field IDs to include

    Returns:
        Dictionary with only the requested fields
    """
    row: dict[str, Any] = {"id": str(employee.id)}

    # Field mapping from API field ID to employee attribute
    # Must match all fields in VALID_FIELDS
    field_mapping = {
        # Name fields
        "firstName": "first_name",
        "lastName": "last_name",
        "preferredName": "preferred_name",
        "middleName": "middle_name",
        "displayName": "display_name",
        "employeeNumber": "employee_number",
        # Contact fields
        "workEmail": "work_email",
        "homeEmail": "home_email",
        "workPhone": "work_phone",
        "workPhoneExtension": "work_phone_extension",
        "mobilePhone": "mobile_phone",
        # Date fields
        "hireDate": "hire_date",
        "terminationDate": "termination_date",
        "dateOfBirth": "date_of_birth",
        # Sensitive fields
        "ssn": "ssn",
        "gender": "gender",
        # List fields
        "department": "department",
        "division": "division",
        "location": "location",
        "status": "status",
        "jobTitle": "job_title",
        # Employee reference
        "supervisorId": "supervisor_id",
        # Photo
        "photoUrl": "photo_url",
        # Address fields
        "address1": "address1",
        "address2": "address2",
        "city": "city",
        "state": "state",
        "zipcode": "zipcode",
        "country": "country",
        # Social
        "linkedIn": "linkedin",
        # Personal
        "maritalStatus": "marital_status",
        "ethnicity": "ethnicity",
        # Compensation
        "salary": "salary",
        "payRate": "pay_rate",
        "payPer": "pay_per",
        "payType": "pay_type",
        "paySchedule": "pay_schedule",
    }

    # Fields that need string conversion (IDs, Decimals)
    string_convert_fields = {"supervisorId", "salary", "payRate"}

    for field in fields:
        attr_name = field_mapping.get(field)
        if attr_name and hasattr(employee, attr_name):
            value = getattr(employee, attr_name)
            if value is not None:
                # Format dates
                if hasattr(value, "isoformat"):
                    value = value.isoformat()
                # Convert IDs and Decimals to string for consistency
                elif field in string_convert_fields:
                    value = str(value)
            # Include field even if null (per BUILD_PLAN section 2)
            row[field] = value

    return row


@require_scopes("read:employees")
async def run_custom_report(
    title: str,
    fields: list[str] | None = None,
    _filters: dict[str, Any] | None = None,  # noqa: ARG001
) -> dict[str, Any]:
    """Run a custom employee report."""
    # Get user context and check HR Admin
    _, persona = get_user_context()

    if persona != "hr_admin":
        return {
            "error": {
                "code": 403,
                "message": "Only HR Admin can run custom reports",
            }
        }

    # Validate title
    if not title or not title.strip():
        return {
            "error": {
                "code": 400,
                "message": "Report title is required",
            }
        }

    # Use default fields if none specified
    if not fields:
        fields = list(DEFAULT_CUSTOM_REPORT_FIELDS)

    # Validate all fields exist
    for field in fields:
        if field not in VALID_FIELDS:
            return {
                "error": {
                    "code": 422,
                    "message": f"Field '{field}' does not exist",
                }
            }

    # Build field definitions for response
    field_definitions = [dict(VALID_FIELDS[f]) for f in fields]

    # Query all employees (HR Admin sees all, regardless of status)
    async with get_session() as session:
        result = await session.execute(
            select(Employee).order_by(Employee.last_name, Employee.first_name)
        )
        employees = list(result.scalars().all())

    # Convert employees to report format with selected fields
    employee_rows = [_employee_to_custom_report_row(emp, fields) for emp in employees]

    return {
        "title": title,
        "fields": field_definitions,
        "employees": employee_rows,
        "totalEmployees": len(employee_rows),
    }


__all__ = [
    "run_company_report",
    "get_custom_reports",
    "get_custom_report",
    "run_custom_report",
]
