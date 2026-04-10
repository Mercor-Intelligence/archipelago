"""Tool: bamboo.employees.get_directory

Retrieves the employee directory with field definitions and employee records,
filtered by persona permissions.

BambooHR API: GET /v1/employees/directory

Auth & Permissions:
- HR Admin: All employees, all fields
- Manager: Direct reports only, limited fields
- Employee: Self only, limited fields
"""

from constants import get_fields_by_categories
from db import Employee, get_session
from loguru import logger
from schemas import DirectoryEmployee, DirectoryField, GetDirectoryOutput
from sqlalchemy import select

from .auth_helpers import get_user_context

# Directory fields that all personas can see
DIRECTORY_FIELDS = [
    DirectoryField(id="displayName", type="text", name="Display Name"),
    DirectoryField(id="firstName", type="text", name="First Name"),
    DirectoryField(id="lastName", type="text", name="Last Name"),
    DirectoryField(id="jobTitle", type="list", name="Job Title"),
    DirectoryField(id="workPhone", type="text", name="Work Phone"),
    DirectoryField(id="workPhoneExtension", type="text", name="Work Extension"),
    DirectoryField(id="department", type="list", name="Department"),
    DirectoryField(id="location", type="list", name="Location"),
    DirectoryField(id="workEmail", type="email", name="Work Email"),
]

# Additional fields only HR Admin can see
HR_ADMIN_EXTRA_FIELDS = [
    DirectoryField(id="supervisorId", type="int", name="Reports To"),
    DirectoryField(id="photoUrl", type="text", name="Photo URL"),
    DirectoryField(id="division", type="list", name="Division"),
]

# BUILD_PLAN Section 3.2.1: Persona-Based Field Filtering
# - HR Admin: All fields (no restrictions) - sees ssn, salary, compensation fields
# - Manager: All except Restricted + Compensation fields
# - Employee: Core Identity + Job Info + Contact only

# Categories considered restricted (not included in directory for non-HR Admin)
RESTRICTED_CATEGORIES = {"restricted", "compensation"}

# Categories allowed for Employee persona (most restrictive)
EMPLOYEE_ALLOWED_CATEGORIES = {"core", "personal", "job", "contact", "directory"}


def _get_field_definitions_for_persona(persona: str) -> list[DirectoryField]:
    """Get field definitions filtered by persona permissions.

    Args:
        persona: The user's persona (hr_admin, manager, employee)

    Returns:
        List of DirectoryField definitions based on persona permissions
    """
    # HR Admin gets ALL fields (no restrictions)
    if persona == "hr_admin":
        fields = get_fields_by_categories()
    elif persona == "manager":
        # Manager: Filter out restricted and compensation categories
        fields = get_fields_by_categories(exclude_categories=RESTRICTED_CATEGORIES)
    else:  # persona == "employee"
        # Employee: Only core, personal, job, and contact categories
        fields = get_fields_by_categories(categories=EMPLOYEE_ALLOWED_CATEGORIES)

    if fields:
        return [
            DirectoryField(
                id=f.field_id,
                type=f.field_type,
                name=f.field_name,
            )
            for f in fields
        ]

    # Fall back to default fields
    return DIRECTORY_FIELDS.copy()


async def _get_all_employees(status_filter: str | None = None) -> list[Employee]:
    """Get all employees for HR Admin, optionally filtered by status.

    Args:
        status_filter: Optional status to filter by (Active, Inactive, Terminated).
                      If None, returns all employees regardless of status.
    """
    async with get_session() as session:
        query = select(Employee)
        if status_filter:
            query = query.where(Employee.status == status_filter)
        result = await session.execute(query)
        return list(result.scalars().all())


async def _get_direct_reports(
    supervisor_id: int, status_filter: str | None = None
) -> list[Employee]:
    """Get direct reports for a manager, optionally filtered by status.

    Args:
        supervisor_id: The manager's employee ID.
        status_filter: Optional status to filter by. If None, returns all statuses.
    """
    async with get_session() as session:
        query = select(Employee).where(Employee.supervisor_id == supervisor_id)
        if status_filter:
            query = query.where(Employee.status == status_filter)
        result = await session.execute(query)
        return list(result.scalars().all())


async def _get_employee_by_id(
    employee_id: int, status_filter: str | None = None
) -> Employee | None:
    """Get a single employee by ID, optionally filtered by status.

    Args:
        employee_id: The employee's ID.
        status_filter: Optional status to filter by. If None, returns regardless of status.
    """
    async with get_session() as session:
        query = select(Employee).where(Employee.id == employee_id)
        if status_filter:
            query = query.where(Employee.status == status_filter)
        result = await session.execute(query)
        return result.scalar_one_or_none()


def _filter_employee_fields(
    employee: DirectoryEmployee, requested_fields: set[str]
) -> DirectoryEmployee:
    """Filter employee data to only include requested fields.

    Args:
        employee: Full DirectoryEmployee object
        requested_fields: Set of field names to include

    Returns:
        New DirectoryEmployee with only requested fields (plus id which is always included)
    """
    # Always include id for navigation
    data: dict[str, str | None] = {"id": employee.id}

    for field in requested_fields:
        # Skip non-string field names to prevent TypeError
        if not isinstance(field, str):
            continue
        if hasattr(employee, field):
            value = getattr(employee, field)
            # For preferredName, fall back to firstName if not set
            if field == "preferredName" and not value:
                value = employee.firstName
            data[field] = value

    return DirectoryEmployee(**data)


async def get_directory_for_persona(
    requested_fields: list[str] | None = None,
    status_filter: str | None = None,
) -> GetDirectoryOutput:
    """Get employee directory based on persona permissions.

    Args:
        requested_fields: Optional list of fields to include in response.
                         If None, all persona-allowed fields are returned.
        status_filter: Optional status to filter by (Active, Inactive, Terminated).
                      If None, returns all employees regardless of status.
    """
    employee_id, persona = get_user_context()

    if persona == "unknown":
        raise PermissionError("Invalid persona: No recognized role found")

    logger.info(
        f"Getting directory: persona={persona}, employee_id={employee_id}, "
        f"status_filter={status_filter}"
    )

    # Get field definitions based on persona
    fields = _get_field_definitions_for_persona(persona)

    # HR Admin gets extra organizational fields
    if persona == "hr_admin":
        existing_ids = {f.id for f in fields}
        for extra_field in HR_ADMIN_EXTRA_FIELDS:
            if extra_field.id not in existing_ids:
                fields.append(extra_field)

    # Get employees based on persona
    employees: list[Employee] = []

    if persona == "hr_admin":
        # HR Admin sees all employees (optionally filtered by status)
        employees = await _get_all_employees(status_filter)
        logger.info(f"HR Admin: Retrieved {len(employees)} employees")

    elif persona == "manager":
        # Manager sees only direct reports
        if employee_id:
            employees = await _get_direct_reports(employee_id, status_filter)
            logger.info(f"Manager {employee_id}: Retrieved {len(employees)} direct reports")
        else:
            logger.warning("Manager persona without employee_id")

    elif persona == "employee":
        # Employee sees only self
        if employee_id:
            employee = await _get_employee_by_id(employee_id, status_filter)
            if employee:
                employees = [employee]
                logger.info(f"Employee {employee_id}: Retrieved self")
            else:
                logger.info(f"Employee {employee_id}: Not found in database")
        else:
            logger.warning("Employee persona without employee_id")

    # Convert employees to DirectoryEmployee schema
    directory_employees = [DirectoryEmployee.from_employee(emp) for emp in employees]

    # Apply field filtering if requested
    if requested_fields:
        requested_set = set(requested_fields)
        # SECURITY: Intersect requested fields with persona-allowed fields to prevent
        # unauthorized access to HR-Admin-only fields (supervisorId, photoUrl, division)
        allowed_field_ids = {f.id for f in fields}
        permitted_fields = requested_set & allowed_field_ids
        # Filter employee data to only include permitted fields
        directory_employees = [
            _filter_employee_fields(emp, permitted_fields) for emp in directory_employees
        ]
        # Filter field definitions to only include requested fields (plus id)
        fields = [f for f in fields if f.id in requested_set or f.id == "id"]

    return GetDirectoryOutput(
        fields=fields,
        employees=directory_employees,
    )


# Export for registration in main.py
__all__ = ["get_directory_for_persona"]
