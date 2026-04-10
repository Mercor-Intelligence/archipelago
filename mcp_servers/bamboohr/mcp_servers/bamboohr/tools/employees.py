"""Employee tools for BambooHR MCP server.

Implements:
- bamboo.employees.get: Get single employee by ID
- bamboo.employees.update: Update employee fields with persona-based restrictions
- bamboo.employees.get_company_info: Get company metadata

Persona-based access control (per BUILD_PLAN section 3.2.2/3.2.4):
- HR Admin: Can view/edit any employee, all fields
- Manager: Can view/edit self + direct reports, limited fields (no compensation/SSN)
- Employee: Can view self only, Core Identity + Job Info + Contact only (cannot edit)

Note: update_employee uses camelCase parameter names to match the MCP/API interface.
These are intentional and suppressed via # noqa: N803 in pyproject.toml per-file-ignores.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from db import AuditLog, Employee, TimeOffRequest, TimeOffRequestStatus, get_session
from loguru import logger
from mcp_auth import require_scopes, user_has_role
from repositories import EmployeeNotFoundError, EmployeeRepository
from sqlalchemy import select

from .auth_helpers import get_user_context
from .constants import (
    ALL_FIELDS,
    DEFAULT_DIRECTORY_FIELDS,
    EMPLOYEE_ALLOWED_FIELDS,
    FIELD_ALIAS_MAP,
    HR_ADMIN_UPDATABLE_FIELDS,
    IMMUTABLE_FIELDS,
    MANAGER_UPDATABLE_FIELDS,
    RESTRICTED_FIELDS,
    UNIQUE_FIELDS,
    UPDATABLE_FIELDS_CAMELCASE,
)


def _normalize_field_name(field: str) -> str:
    """Convert camelCase field name to snake_case."""
    return FIELD_ALIAS_MAP.get(field, field)


def _parse_fields(fields_input: str | list[str] | None) -> set[str] | None:
    """Parse fields input into a set of normalized field names.

    Args:
        fields_input: Field names as comma-separated string or list of strings
                      (can be camelCase or snake_case)

    Returns:
        Set of snake_case field names, or None for all fields
    """
    if not fields_input:
        return None

    # Handle both list and comma-separated string formats
    if isinstance(fields_input, list):
        field_list = fields_input
    else:
        field_list = [f.strip() for f in fields_input.split(",")]

    fields = set()
    for field in field_list:
        field = field.strip() if isinstance(field, str) else field
        if field:
            normalized = _normalize_field_name(field)
            if normalized in ALL_FIELDS:
                fields.add(normalized)
    return fields if fields else None


def _get_allowed_fields_for_persona(persona: str) -> set[str]:
    """Get the set of fields a persona is allowed to view.

    Per BUILD_PLAN section 3.2.2:
    - HR Admin: All fields (no restrictions)
    - Manager: All except Restricted + Compensation fields
    - Employee: Core Identity + Job Info + Contact only

    Args:
        persona: The user's persona (hr_admin, manager, employee)

    Returns:
        Set of snake_case field names allowed for this persona
    """
    if persona == "hr_admin":
        return ALL_FIELDS
    elif persona == "manager":
        # Manager: All fields except restricted/compensation
        return ALL_FIELDS - RESTRICTED_FIELDS
    else:  # employee
        # Employee: Only core identity, job info, and contact fields
        return EMPLOYEE_ALLOWED_FIELDS


def _filter_employee_fields(
    employee: Employee,
    requested_fields: set[str] | None,
    persona: str,
) -> dict[str, Any]:
    """Filter employee fields based on requested fields and persona permissions.

    Args:
        employee: Employee model instance
        requested_fields: Set of field names to include, or None for all
        persona: The user's persona (hr_admin, manager, employee)

    Returns:
        Dictionary of field name -> value for the employee
    """
    result: dict[str, Any] = {"id": str(employee.id)}

    # Get fields allowed for this persona
    allowed_fields = _get_allowed_fields_for_persona(persona)

    # Determine which fields to include (intersection of requested and allowed)
    if requested_fields:
        fields_to_include = requested_fields & allowed_fields
    else:
        fields_to_include = allowed_fields

    for field in fields_to_include:
        # Skip 'id' as we always include it
        if field == "id":
            continue

        # Get field value from employee model
        if hasattr(employee, field):
            value = getattr(employee, field)
            # Convert dates to ISO format strings
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            # Convert Decimal to string for JSON serialization
            elif hasattr(value, "__class__") and value.__class__.__name__ == "Decimal":
                value = str(value)
            result[field] = value

    return result


def _employee_to_response(
    employee: Employee,
    requested_fields: set[str] | None,
    persona: str,
) -> dict[str, Any]:
    """Convert employee model to API response format.

    Args:
        employee: Employee model instance
        requested_fields: Set of field names to include, or None for all
        persona: The user's persona (hr_admin, manager, employee)

    Returns:
        Dictionary in BambooHR API response format
    """
    data = _filter_employee_fields(employee, requested_fields, persona)

    # Convert snake_case to camelCase for API response
    response = {}
    reverse_alias_map = {v: k for k, v in FIELD_ALIAS_MAP.items()}

    for key, value in data.items():
        # Use camelCase alias if available
        api_key = reverse_alias_map.get(key, key)
        response[api_key] = value

    return response


async def _is_direct_report(manager_id: int, employee_id: int) -> bool:
    """Check if employee is a direct report of the manager.

    Args:
        manager_id: ID of the manager
        employee_id: ID of the employee to check

    Returns:
        True if employee reports to manager, False otherwise
    """
    async with get_session() as session:
        result = await session.execute(
            select(Employee.id).where(
                Employee.id == employee_id,
                Employee.supervisor_id == manager_id,
            )
        )
        return result.scalar_one_or_none() is not None


async def _check_access(
    target_employee_id: int,
    user_employee_id: int | None,
) -> tuple[bool, str | None]:
    """Check if user has access to view the target employee.

    Args:
        target_employee_id: ID of employee being requested
        user_employee_id: ID of the requesting user's employee record

    Returns:
        Tuple of (has_access, error_message)
    """
    # HR Admin can view anyone (also returns True if auth disabled)
    if user_has_role("hr_admin"):
        return True, None

    # Manager can view self + direct reports
    if user_has_role("manager"):
        # Can view self
        if user_employee_id == target_employee_id:
            return True, None
        # Can view direct reports (use 'is not None' to handle employee_id=0)
        if user_employee_id is not None and await _is_direct_report(
            user_employee_id, target_employee_id
        ):
            return True, None
        return False, "Access denied: can only view self or direct reports"

    # Employee can only view self
    if user_has_role("employee"):
        if user_employee_id == target_employee_id:
            return True, None
        return False, "Access denied: can only view own profile"

    # No recognized role
    return False, "Access denied: no valid role"


@require_scopes("read:employees")
async def get_employee(
    employee_id: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Get a single employee by ID."""
    # Get user context from middleware
    user_employee_id, persona = get_user_context()

    # Validate employee_id
    try:
        emp_id = int(employee_id)
    except ValueError as exc:
        raise ValueError(f"Invalid employee ID: {employee_id}") from exc

    # Handle employee_id "0" for self
    if emp_id == 0:
        if user_employee_id is None:
            return {
                "error": {
                    "code": 400,
                    "message": "Cannot resolve self: no employee ID in token",
                }
            }
        emp_id = user_employee_id

    if emp_id < 0:
        raise ValueError(f"Invalid employee ID: {employee_id}")

    # Check access permissions (Layer 2: persona filtering)
    has_access, error_msg = await _check_access(emp_id, user_employee_id)
    if not has_access:
        return {"error": {"code": 403, "message": error_msg}}

    # Parse requested fields
    requested_fields = _parse_fields(fields)

    # Get employee from database
    async with get_session() as session:
        repo = EmployeeRepository(session)
        try:
            employee = await repo.get_by_id(emp_id)
        except EmployeeNotFoundError:
            return {"error": {"code": 404, "message": f"Employee {emp_id} not found"}}

    # Convert to response format with persona-based field filtering
    # Per BUILD_PLAN section 3.2.2:
    # - HR Admin: All fields
    # - Manager: All except Restricted + Compensation (even for self)
    # - Employee: Core Identity + Job Info + Contact only (even for self)
    return _employee_to_response(employee, requested_fields, persona)


# ============================================================================
# Update Employee Tool
# ============================================================================


def _get_updatable_fields_for_persona(persona: str) -> set[str]:
    """Get the set of fields a persona is allowed to update.

    Per BUILD_PLAN section 3.2.4:
    - HR Admin: All fields except immutable (id) - includes hireDate
    - Manager: Job info, contact, address, supervisorId, status (no compensation/SSN/hireDate)
    - Employee: Cannot update (returns empty set)

    Args:
        persona: The user's persona (hr_admin, manager, employee)

    Returns:
        Set of snake_case field names this persona can update
    """
    if persona == "hr_admin":
        return HR_ADMIN_UPDATABLE_FIELDS
    elif persona == "manager":
        return MANAGER_UPDATABLE_FIELDS
    else:
        # Employees cannot update any fields
        return set()


async def _check_update_access(
    target_employee_id: int,
    user_employee_id: int | None,
) -> tuple[bool, str | None]:
    """Check if user has access to update the target employee.

    Args:
        target_employee_id: ID of employee being updated
        user_employee_id: ID of the requesting user's employee record

    Returns:
        Tuple of (has_access, error_message)
    """
    # HR Admin can update anyone (also returns True if auth disabled)
    if user_has_role("hr_admin"):
        return True, None

    # Manager can only update direct reports (not self for updates)
    if user_has_role("manager"):
        if user_employee_id is not None and await _is_direct_report(
            user_employee_id, target_employee_id
        ):
            return True, None
        return False, "Access denied: can only update direct reports"

    # Employee cannot update anyone
    if user_has_role("employee"):
        return False, "Access denied: employees cannot update records"

    # No recognized role
    return False, "Access denied: no valid role"


async def _check_unique_field(
    session, field_name: str, value: Any, exclude_employee_id: int
) -> str | None:
    """Check if a unique field value is already in use.

    Args:
        session: Database session
        field_name: Name of the field to check (snake_case)
        value: The value to check for uniqueness
        exclude_employee_id: Employee ID to exclude from check (the one being updated)

    Returns:
        Error message if duplicate found, None otherwise
    """
    if value is None:
        return None

    query = select(Employee.id).where(
        getattr(Employee, field_name) == value,
        Employee.id != exclude_employee_id,
    )
    result = await session.execute(query)
    existing = result.scalar_one_or_none()

    if existing is not None:
        field_display = FIELD_ALIAS_MAP.get(field_name, field_name)
        if field_name == "work_email":
            return "Email already in use"
        elif field_name == "employee_number":
            return "Employee number already in use"
        return f"{field_display} already in use"

    return None


async def _check_circular_supervisor(
    session, employee_id: int, new_supervisor_id: int
) -> str | None:
    """Check for circular supervisor relationships.

    Args:
        session: Database session
        employee_id: ID of employee being updated
        new_supervisor_id: Proposed new supervisor ID

    Returns:
        Error message if circular relationship detected, None otherwise
    """
    # Employee cannot supervise themselves
    if employee_id == new_supervisor_id:
        return "Employee cannot supervise themselves"

    # Check if the new supervisor is in the employee's management chain
    # (i.e., the new supervisor reports to this employee directly or indirectly)
    current_id = new_supervisor_id
    visited = {employee_id}  # Start with the employee being updated

    while current_id is not None:
        if current_id in visited:
            return "Circular supervisor relationship detected"

        visited.add(current_id)

        # Get the supervisor's supervisor
        result = await session.execute(
            select(Employee.supervisor_id).where(Employee.id == current_id)
        )
        supervisor_id = result.scalar_one_or_none()
        current_id = supervisor_id

    return None


async def _check_active_time_off(session, employee_id: int) -> bool:
    """Check if employee has active (pending/approved) time-off requests.

    Args:
        session: Database session
        employee_id: Employee ID to check

    Returns:
        True if employee has active time-off requests, False otherwise
    """
    result = await session.execute(
        select(TimeOffRequest.id)
        .where(
            TimeOffRequest.employee_id == employee_id,
            TimeOffRequest.status.in_(
                [
                    TimeOffRequestStatus.REQUESTED.value,
                    TimeOffRequestStatus.APPROVED.value,
                ]
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _parse_update_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Parse and normalize update fields from input data.

    Converts camelCase field names to snake_case and filters out
    non-employee fields (like employeeId).

    Args:
        data: Input dictionary with field names and values

    Returns:
        Dictionary with snake_case field names and values
    """
    normalized = {}
    for key, value in data.items():
        if key in ("employeeId", "employee_id"):
            continue  # Skip the ID field, it's not updatable

        # Normalize to snake_case
        snake_key = _normalize_field_name(key)

        # Only include recognized fields
        if snake_key in ALL_FIELDS:
            normalized[snake_key] = value

    return normalized


def _convert_value_for_db(field_name: str, value: Any) -> Any:
    """Convert input value to appropriate database type.

    Args:
        field_name: Field name (snake_case)
        value: Input value

    Returns:
        Converted value suitable for database storage

    Raises:
        ValueError: If value cannot be converted to the expected type
    """
    if value is None:
        return None

    # Handle empty string as None for nullable fields
    if isinstance(value, str) and value.strip() == "":
        return None

    # Decimal fields
    if field_name in ("salary", "pay_rate"):
        if isinstance(value, (int, float, str)):
            try:
                return Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(f"Invalid value for {field_name}: {value}") from exc
        return value

    # Integer fields
    if field_name == "supervisor_id":
        if isinstance(value, str):
            return int(value) if value else None
        return value

    # Date fields
    if field_name in ("date_of_birth", "termination_date", "hire_date"):
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"Invalid date format for {field_name}: {value}") from exc
        return value

    return value


async def _record_audit_log(
    session,
    employee_id: int,
    actor_id: int | None,
    persona: str,
    old_values: dict[str, Any],
    new_values: dict[str, Any],
) -> None:
    """Record an audit log entry for the update.

    Args:
        session: Database session
        employee_id: ID of employee being updated
        actor_id: ID of user performing the update
        persona: Persona of the actor
        old_values: Previous field values
        new_values: New field values being set
    """
    audit_entry = AuditLog(
        action="update",
        entity_type="employee",
        entity_id=employee_id,
        actor_id=actor_id,
        actor_persona=persona,
        old_values=old_values,
        new_values=new_values,
    )
    session.add(audit_entry)


@require_scopes("write:employees")
async def update_employee(
    employeeId: str,
    firstName: str | None = None,
    lastName: str | None = None,
    preferredName: str | None = None,
    middleName: str | None = None,
    displayName: str | None = None,
    workEmail: str | None = None,
    homeEmail: str | None = None,
    workPhone: str | None = None,
    workPhoneExtension: str | None = None,
    mobilePhone: str | None = None,
    address1: str | None = None,
    address2: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zipcode: str | None = None,
    country: str | None = None,
    department: str | None = None,
    division: str | None = None,
    jobTitle: str | None = None,
    location: str | None = None,
    status: str | None = None,
    supervisorId: str | None = None,
    photoUrl: str | None = None,
    linkedIn: str | None = None,
    ssn: str | None = None,
    dateOfBirth: str | None = None,
    gender: str | None = None,
    maritalStatus: str | None = None,
    ethnicity: str | None = None,
    salary: str | None = None,
    payRate: str | None = None,
    payPer: str | None = None,
    payType: str | None = None,
    paySchedule: str | None = None,
    employeeNumber: str | None = None,
    terminationDate: str | None = None,
    hireDate: str | None = None,  # noqa: N803
) -> dict[str, Any]:
    """Update specified fields on an existing employee record."""
    # Build fields dict from explicit parameters for existing logic.
    # Semantic distinction:
    #   - None (default) = parameter not provided, don't update this field
    #   - "" (empty string) = explicitly clear this field's value
    # Empty strings are converted to None in _convert_value_for_db() for DB storage.
    fields: dict[str, Any] = {}
    local_values = locals()
    for field_name in UPDATABLE_FIELDS_CAMELCASE:
        value = local_values.get(field_name)
        if value is not None:
            fields[field_name] = value

    # Get user context from middleware
    user_employee_id, persona = get_user_context()

    logger.debug(f"[update_employee] User: {user_employee_id}, Persona: {persona}")
    logger.debug(f"[update_employee] Input fields: {list(fields.keys())}")

    # Validate employee_id
    try:
        emp_id = int(employeeId)
    except (ValueError, TypeError):
        return {"error": {"code": 400, "message": f"Invalid employee ID: {employeeId}"}}

    if emp_id <= 0:
        return {"error": {"code": 400, "message": f"Invalid employee ID: {employeeId}"}}

    # Parse and normalize update fields early (before DB access)
    update_fields = _parse_update_fields(fields)

    if not update_fields:
        return {"error": {"code": 400, "message": "No fields to update"}}

    logger.debug(f"[update_employee] Normalized fields: {list(update_fields.keys())}")

    # Check for immutable fields
    for field in update_fields:
        if field in IMMUTABLE_FIELDS or _normalize_field_name(field) in IMMUTABLE_FIELDS:
            return {
                "error": {
                    "code": 400,
                    "message": f"Cannot update immutable field: {field}",
                }
            }

    # Filter fields based on persona permissions
    allowed_fields = _get_updatable_fields_for_persona(persona)
    restricted_fields = set(update_fields.keys()) - allowed_fields

    if restricted_fields:
        # Return error for restricted fields (per BUILD_PLAN edge case handling)
        first_restricted = next(iter(restricted_fields))
        return {
            "error": {
                "code": 403,
                "message": f"Cannot update field: {first_restricted}",
            }
        }

    # Perform the update within a transaction
    async with get_session() as session:
        # Get the employee first to ensure proper 404 for non-existent employees.
        # This must happen before access check so managers get 404 (not 403).
        repo = EmployeeRepository(session)
        try:
            employee = await repo.get_by_id(emp_id)
        except EmployeeNotFoundError:
            return {"error": {"code": 404, "message": f"Employee {emp_id} not found"}}

        # Check access permissions (after existence check)
        has_access, error_msg = await _check_update_access(emp_id, user_employee_id)
        if not has_access:
            return {"error": {"code": 403, "message": error_msg}}

        # Track old values for audit log
        old_values = {}
        for field in update_fields:
            if hasattr(employee, field):
                old_val = getattr(employee, field)
                # Convert for JSON serialization
                if hasattr(old_val, "isoformat"):
                    old_val = old_val.isoformat()
                elif isinstance(old_val, Decimal):
                    old_val = str(old_val)
                old_values[field] = old_val

        # Validate unique fields
        for field in UNIQUE_FIELDS:
            if field in update_fields:
                error = await _check_unique_field(session, field, update_fields[field], emp_id)
                if error:
                    return {"error": {"code": 409, "message": error}}

        # Validate supervisor relationship
        if "supervisor_id" in update_fields:
            new_supervisor_id = update_fields["supervisor_id"]
            # Treat empty/whitespace string as None (clear supervisor)
            if isinstance(new_supervisor_id, str) and not new_supervisor_id.strip():
                new_supervisor_id = None
                update_fields["supervisor_id"] = None

            if new_supervisor_id is not None:
                # Convert string to int if needed
                if isinstance(new_supervisor_id, str):
                    try:
                        new_supervisor_id = int(new_supervisor_id)
                    except ValueError:
                        return {
                            "error": {
                                "code": 400,
                                "message": "Invalid supervisor ID format",
                            }
                        }
                    update_fields["supervisor_id"] = new_supervisor_id

                # Check supervisor exists
                try:
                    await repo.get_by_id(new_supervisor_id)
                except EmployeeNotFoundError:
                    return {
                        "error": {
                            "code": 400,
                            "message": f"Supervisor {new_supervisor_id} not found",
                        }
                    }

                # Check for circular relationship
                error = await _check_circular_supervisor(session, emp_id, new_supervisor_id)
                if error:
                    return {"error": {"code": 422, "message": error}}

        # Validate status change (case-insensitive comparison)
        if "status" in update_fields:
            new_status = update_fields["status"]
            new_status_lower = new_status.lower() if isinstance(new_status, str) else ""
            current_status_lower = (
                employee.status.lower() if isinstance(employee.status, str) else ""
            )

            # Validate status is a known value
            valid_statuses = {"active", "inactive", "terminated"}
            if new_status_lower not in valid_statuses:
                return {
                    "error": {
                        "code": 400,
                        "message": f"Invalid status: {new_status}. "
                        "Must be Active, Inactive, or Terminated",
                    }
                }

            # Check PTO when deactivating or terminating an active employee
            if new_status_lower in ("inactive", "terminated") and current_status_lower == "active":
                has_active_pto = await _check_active_time_off(session, emp_id)
                if has_active_pto:
                    return {
                        "error": {
                            "code": 422,
                            "message": "Cannot deactivate employee with pending time-off requests",
                        }
                    }

            # Normalize status to proper case for storage
            if new_status_lower == "active":
                update_fields["status"] = "Active"
            elif new_status_lower == "inactive":
                update_fields["status"] = "Inactive"
            elif new_status_lower == "terminated":
                update_fields["status"] = "Terminated"

        # Convert all values BEFORE modifying employee (atomicity guarantee)
        # This ensures no partial updates if a later field fails conversion
        converted_fields: dict[str, Any] = {}
        for field, value in update_fields.items():
            try:
                converted_fields[field] = _convert_value_for_db(field, value)
            except ValueError as e:
                return {"error": {"code": 400, "message": str(e)}}

        # Apply all updates (safe now that all conversions succeeded)
        new_values = {}
        for field, converted_value in converted_fields.items():
            setattr(employee, field, converted_value)
            # Store new value for audit
            if hasattr(converted_value, "isoformat"):
                new_values[field] = converted_value.isoformat()
            elif isinstance(converted_value, Decimal):
                new_values[field] = str(converted_value)
            else:
                new_values[field] = converted_value

        # Record audit log
        await _record_audit_log(session, emp_id, user_employee_id, persona, old_values, new_values)

        # Commit the transaction
        await session.commit()

        field_names = list(new_values.keys())
        logger.info(f"[update_employee] Updated employee {emp_id}, fields: {field_names}")

        return {
            "success": True,
            "updated": datetime.now(UTC).isoformat(),
        }


# ============================================================================
# Get Company Info Tool
# ============================================================================


@require_scopes("read:employees")
async def get_company_info() -> dict[str, Any]:
    """Get company metadata including directory settings."""
    return {
        "title": "Acme Corp",
        "fields": DEFAULT_DIRECTORY_FIELDS,
    }


__all__ = ["get_employee", "update_employee", "get_company_info"]
