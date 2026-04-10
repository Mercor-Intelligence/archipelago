"""Time-off policy tools for BambooHR MCP server.

Implements:
- bamboo.time_off.get_policies: List all time-off policies (#54)
- bamboo.time_off.get_employee_policies: Get policies for an employee (#55)
- bamboo.time_off.assign_policy: Assign/remove policies for an employee (#56)
- bamboo.time_off.create_policy: Create a new time-off policy (HR Admin only)

Persona-based access control:
- HR Admin: Full access to all policies and assignments
- Manager: Can view policies assigned to direct reports
- Employee: Can view own assigned policies
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from db import (
    Employee,
    EmployeePolicy,
    TimeOffBalance,
    TimeOffPolicy,
    TimeOffRequest,
    TimeOffRequestStatus,
    TimeOffType,
    get_session,
)
from loguru import logger
from mcp_auth import require_roles, require_scopes, user_has_role
from schemas import (
    AssignedPolicyEntry,
    AssignPolicyResponse,
    CreateTimeOffPolicyRequest,
    CreateTimeOffPolicyResponse,
    EmployeePolicyEntry,
    PolicyAssignmentInput,
    PolicyListEntry,
    RemovedPolicyEntry,
)
from sqlalchemy import select

from .auth_helpers import get_user_context


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


# ============================================================================
# Get Policies Tool (#54)
# ============================================================================


@require_scopes("read:time_off")
async def get_policies() -> list[dict]:
    """Get all time-off policies."""
    async with get_session() as session:
        # Query policies with their associated time-off type
        result = await session.execute(
            select(TimeOffPolicy, TimeOffType.id.label("type_id")).join(
                TimeOffType, TimeOffPolicy.type_id == TimeOffType.id
            )
        )
        rows = result.all()

        policies = []
        for row in rows:
            policy = row[0]  # TimeOffPolicy object
            entry = PolicyListEntry(
                id=policy.id,
                timeOffTypeId=policy.type_id,
                name=policy.name,
                effectiveDate=None,  # Policy-level effective date (not per-employee)
                type=policy.accrual_type,  # "accruing", "manual", "discretionary"
            )
            policies.append(entry.model_dump(by_alias=True))

        return policies


# ============================================================================
# Get Employee Policies Tool (#55)
# ============================================================================


async def _check_employee_access(
    target_employee_id: int,
    user_employee_id: int | None,
) -> tuple[bool, str | None]:
    """Check if user has access to view the target employee's policies.

    Args:
        target_employee_id: ID of employee whose policies are requested
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
        # Can view direct reports
        if user_employee_id is not None and await _is_direct_report(
            user_employee_id, target_employee_id
        ):
            return True, None
        return False, "Access denied: can only view self or direct reports"

    # Employee can only view self
    if user_has_role("employee"):
        if user_employee_id == target_employee_id:
            return True, None
        return False, "Access denied: can only view own policies"

    # No recognized role
    return False, "Access denied: no valid role"


@require_scopes("read:time_off")
async def get_employee_policies(employeeId: str) -> list[dict] | dict[str, Any]:  # noqa: N803
    """Get time-off policies assigned to a specific employee."""
    # Get user context from middleware
    user_employee_id, persona = get_user_context()

    # Validate employee_id
    try:
        emp_id = int(employeeId)
    except ValueError:
        return {"error": {"code": 400, "message": f"Invalid employee ID: {employeeId}"}}

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
        return {"error": {"code": 400, "message": f"Invalid employee ID: {employeeId}"}}

    async with get_session() as session:
        # Check employee exists
        employee_result = await session.execute(select(Employee.id).where(Employee.id == emp_id))
        if employee_result.scalar_one_or_none() is None:
            return {"error": {"code": 404, "message": f"Employee {emp_id} not found"}}

        # Check access permissions
        has_access, error_msg = await _check_employee_access(emp_id, user_employee_id)
        if not has_access:
            return {"error": {"code": 403, "message": error_msg}}

        # Query assigned policies with joins to get full details
        today = date.today()
        result = await session.execute(
            select(
                EmployeePolicy,
                TimeOffPolicy.name.label("policy_name"),
                TimeOffType.id.label("type_id"),
                TimeOffType.name.label("type_name"),
            )
            .join(TimeOffPolicy, EmployeePolicy.policy_id == TimeOffPolicy.id)
            .join(TimeOffType, TimeOffPolicy.type_id == TimeOffType.id)
            .where(
                EmployeePolicy.employee_id == emp_id,
                # Only active policies (end_date is null or strictly in the future)
                (EmployeePolicy.end_date.is_(None) | (EmployeePolicy.end_date > today)),
            )
        )
        rows = result.all()

        policies = []
        for row in rows:
            emp_policy = row[0]  # EmployeePolicy object
            entry = EmployeePolicyEntry(
                timeOffTypeId=row.type_id,
                timeOffTypeName=row.type_name,
                policyId=emp_policy.policy_id,
                policyName=row.policy_name,
                effectiveDate=emp_policy.effective_date.isoformat(),
            )
            policies.append(entry.model_dump(by_alias=True))

        logger.debug(
            f"[get_employee_policies] Employee {emp_id}: found {len(policies)} active policies"
        )

        return policies


# ============================================================================
# Assign Policy Tool (#56)
# ============================================================================


async def _check_pending_requests(session, employee_id: int, policy_id: int) -> bool:
    """Check if employee has pending (future/current) time-off requests for a policy.

    Only checks requests where end_date is today or in the future.
    Historical requests (already completed) don't block policy removal.

    Args:
        session: Database session
        employee_id: Employee ID
        policy_id: Policy ID to check

    Returns:
        True if pending requests exist, False otherwise
    """
    today = date.today()
    result = await session.execute(
        select(TimeOffRequest.id)
        .where(
            TimeOffRequest.employee_id == employee_id,
            TimeOffRequest.policy_id == policy_id,
            TimeOffRequest.status.in_(
                [
                    TimeOffRequestStatus.REQUESTED.value,
                    TimeOffRequestStatus.APPROVED.value,
                ]
            ),
            # Only consider future/current requests (end_date >= today)
            TimeOffRequest.end_date >= today,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


@require_roles("hr_admin")
@require_scopes("write:time_off")
async def assign_policy(
    employeeId: str,  # noqa: N803
    policies: list[PolicyAssignmentInput],
) -> dict:
    """Assign or remove time-off policies for an employee."""
    # Get user context from middleware
    user_employee_id, persona = get_user_context()

    logger.debug(f"[assign_policy] User: {user_employee_id}, Persona: {persona}")
    logger.debug(f"[assign_policy] Employee: {employeeId}, Policies: {policies}")

    # Validate employee_id
    try:
        emp_id = int(employeeId)
    except ValueError:
        return {"error": {"code": 400, "message": f"Invalid employee ID: {employeeId}"}}

    if emp_id <= 0:
        return {"error": {"code": 400, "message": f"Invalid employee ID: {employeeId}"}}

    async with get_session() as session:
        # Get employee with hire date
        employee_result = await session.execute(select(Employee).where(Employee.id == emp_id))
        employee = employee_result.scalar_one_or_none()
        if employee is None:
            return {"error": {"code": 404, "message": f"Employee {emp_id} not found"}}

        # Process policies - validate and convert dicts to PolicyAssignmentInput objects
        # Separate into assignments (valid date) and explicit removals ("0000-00-00"/null)
        # Each assignment tuple is (policy_id, effective_date, type_id)
        new_assignments: list[tuple[int, date, int]] = []
        explicit_removals: list[int] = []  # Policy IDs explicitly marked for removal
        all_policy_ids: list[int] = []  # Track all policy IDs for duplicate check

        for policy_input in policies:
            # Handle dict inputs, PolicyAssignmentInput objects, and reject invalid types
            if isinstance(policy_input, PolicyAssignmentInput):
                validated = policy_input
            elif isinstance(policy_input, dict):
                try:
                    validated = PolicyAssignmentInput.model_validate(policy_input)
                except Exception as e:
                    # Extract meaningful error message from Pydantic validation
                    error_msg = str(e)
                    is_missing = "required" in error_msg.lower() or "missing" in error_msg.lower()

                    # Check which field has the error and whether it's missing or invalid format
                    if "timeOffPolicyId" in error_msg or "time_off_policy_id" in error_msg:
                        if is_missing:
                            msg = "timeOffPolicyId is required for each policy"
                        else:
                            msg = "timeOffPolicyId must be a valid integer"
                        return {"error": {"code": 400, "message": msg}}

                    if "accrualStartDate" in error_msg or "accrual_start_date" in error_msg:
                        if is_missing:
                            msg = "accrualStartDate is required for each policy"
                        else:
                            msg = "accrualStartDate must be a valid date (YYYY-MM-DD)"
                        return {"error": {"code": 400, "message": msg}}

                    return {
                        "error": {
                            "code": 400,
                            "message": f"Invalid policy input: {error_msg}",
                        }
                    }
            else:
                input_type = type(policy_input).__name__
                return {
                    "error": {
                        "code": 400,
                        "message": f"Invalid policy input: expected object, got {input_type}",
                    }
                }

            policy_id = validated.time_off_policy_id
            all_policy_ids.append(policy_id)

            # Verify policy exists and get its type_id
            policy_result = await session.execute(
                select(TimeOffPolicy).where(TimeOffPolicy.id == policy_id)
            )
            policy = policy_result.scalar_one_or_none()
            if policy is None:
                return {
                    "error": {
                        "code": 404,
                        "message": f"Policy {policy_id} not found",
                    }
                }
            policy_type_id = policy.type_id

            # Check if this is a removal request ("0000-00-00" or null accrualStartDate)
            if validated.is_removal:
                explicit_removals.append(policy_id)
                continue  # Skip assignment validation for removals

            effective_date = validated.accrual_start_date

            # Validate employee has a hire date (only for assignments, not removals)
            if employee.hire_date is None:
                return {
                    "error": {
                        "code": 422,
                        "message": "Employee must have hire date before assigning policies",
                    }
                }

            # Validate effective date is on or after hire date
            if effective_date < employee.hire_date:
                return {
                    "error": {
                        "code": 422,
                        "message": (
                            f"Accrual start date ({effective_date.isoformat()}) "
                            f"cannot be before hire date ({employee.hire_date.isoformat()})"
                        ),
                    }
                }

            new_assignments.append((policy_id, effective_date, policy_type_id))

        # Check for duplicate policy IDs in input (across both assignments and removals)
        if len(all_policy_ids) != len(set(all_policy_ids)):
            return {
                "error": {
                    "code": 400,
                    "message": "Duplicate policy IDs in request",
                }
            }

        # Check for duplicate time-off type categories in assignments only
        # (removals don't count toward the duplicate type check)
        type_ids = [p[2] for p in new_assignments]
        if len(type_ids) != len(set(type_ids)):
            return {
                "error": {
                    "code": 422,
                    "message": "Cannot assign multiple policies of the same time-off type",
                }
            }

        # Get current active assignments
        # Active = end_date is null or strictly in the future (end_date > today)
        # Policies with end_date == today are considered ended (removed today)
        today = date.today()
        current_result = await session.execute(
            select(EmployeePolicy).where(
                EmployeePolicy.employee_id == emp_id,
                (EmployeePolicy.end_date.is_(None) | (EmployeePolicy.end_date > today)),
            )
        )
        current_assignments = {ep.policy_id: ep for ep in current_result.scalars().all()}

        # Get all ended (historical) assignments for potential reactivation
        # Ended = end_date is not null and end_date <= today
        ended_result = await session.execute(
            select(EmployeePolicy).where(
                EmployeePolicy.employee_id == emp_id,
                EmployeePolicy.end_date.is_not(None),
                EmployeePolicy.end_date <= today,
            )
        )
        # Group by policy_id - there may be multiple ended records per policy
        ended_assignments: dict[int, list[EmployeePolicy]] = {}
        for ep in ended_result.scalars().all():
            if ep.policy_id not in ended_assignments:
                ended_assignments[ep.policy_id] = []
            ended_assignments[ep.policy_id].append(ep)

        # Determine what to add and remove
        # to_add: policies with valid dates that aren't currently assigned
        # to_remove: policies either explicitly marked for removal ("0000-00-00")
        #            OR currently assigned but not in the new assignments list (set-based removal)
        new_assignment_ids = set(p[0] for p in new_assignments)
        current_policy_ids = set(current_assignments.keys())

        to_add = new_assignment_ids - current_policy_ids
        # Combine: explicit removals + policies dropped from the set
        to_remove = (
            set(explicit_removals) | (current_policy_ids - new_assignment_ids)
        ) & current_policy_ids

        # Check for pending requests before removing
        for policy_id in to_remove:
            has_pending = await _check_pending_requests(session, emp_id, policy_id)
            if has_pending:
                return {
                    "error": {
                        "code": 422,
                        "message": (
                            f"Cannot remove policy {policy_id}: "
                            "employee has pending time-off requests"
                        ),
                    }
                }

        # Perform updates atomically
        assigned = []
        removed = []

        # Remove policies (set end_date to today)
        for policy_id in to_remove:
            emp_policy = current_assignments[policy_id]
            emp_policy.end_date = today
            # Get policy details for response
            policy_result = await session.execute(
                select(TimeOffPolicy.name).where(TimeOffPolicy.id == policy_id)
            )
            policy_name = policy_result.scalar_one_or_none()
            entry = RemovedPolicyEntry(
                policyId=policy_id,
                policyName=policy_name,
            )
            removed.append(entry.model_dump(by_alias=True))

        # Add new policies (delete old ended records to avoid unique constraint issues)
        for policy_id, effective_date, _type_id in new_assignments:
            if policy_id in to_add:
                # Delete any ended assignments for this policy to avoid unique constraint
                if policy_id in ended_assignments:
                    for old_record in ended_assignments[policy_id]:
                        await session.delete(old_record)
                    # Flush deletions before adding new record with same key
                    await session.flush()

                # Create new assignment
                new_emp_policy = EmployeePolicy(
                    employee_id=emp_id,
                    policy_id=policy_id,
                    effective_date=effective_date,
                )
                session.add(new_emp_policy)

                # Initialize TimeOffBalance for current year (starting at 0)
                # Only create if one doesn't already exist (e.g., from previous assignment)
                current_year = today.year
                existing_balance = await session.execute(
                    select(TimeOffBalance).where(
                        TimeOffBalance.employee_id == emp_id,
                        TimeOffBalance.policy_id == policy_id,
                        TimeOffBalance.year == current_year,
                    )
                )
                if existing_balance.scalar_one_or_none() is None:
                    new_balance = TimeOffBalance(
                        employee_id=emp_id,
                        policy_id=policy_id,
                        year=current_year,
                        balance=Decimal("0.00"),
                        used=Decimal("0.00"),
                        scheduled=Decimal("0.00"),
                    )
                    session.add(new_balance)

                # Get policy details for response
                policy_result = await session.execute(
                    select(TimeOffPolicy.name).where(TimeOffPolicy.id == policy_id)
                )
                policy_name = policy_result.scalar_one_or_none()
                entry = AssignedPolicyEntry(
                    policyId=policy_id,
                    policyName=policy_name,
                    effectiveDate=effective_date.isoformat(),
                )
                assigned.append(entry.model_dump(by_alias=True))

        # Commit transaction
        await session.commit()

        logger.info(
            f"[assign_policy] Employee {emp_id}: assigned {len(assigned)}, removed {len(removed)}"
        )

        # Build response using Pydantic schema
        response = AssignPolicyResponse(assigned=assigned, removed=removed)
        return response.model_dump(by_alias=True)


@require_roles("hr_admin")
@require_scopes("write:time_off")
async def create_policy(
    request: CreateTimeOffPolicyRequest,
) -> CreateTimeOffPolicyResponse:
    """Create a new time-off policy."""
    async with get_session() as session:
        # Validate type_id exists
        type_result = await session.execute(
            select(TimeOffType).where(TimeOffType.id == request.type_id)
        )
        time_off_type = type_result.scalar_one_or_none()
        if time_off_type is None:
            raise ValueError(f"Time-off type with ID {request.type_id} not found")

        # Check for duplicate name
        existing = await session.execute(
            select(TimeOffPolicy).where(TimeOffPolicy.name == request.name)
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"Time-off policy '{request.name}' already exists")

        # Create new policy
        new_policy = TimeOffPolicy(
            name=request.name,
            type_id=request.type_id,
            accrual_type=request.accrual_type,
            accrual_rate=request.accrual_rate,
            accrual_frequency=request.accrual_frequency,
            max_balance=request.max_balance,
            carry_over=request.carry_over,
            carry_over_max=request.carry_over_max,
        )
        session.add(new_policy)
        await session.flush()  # Get the ID
        await session.commit()

        logger.info(
            f"[create_policy] Created policy '{new_policy.name}' (ID: {new_policy.id}) "
            f"for type {request.type_id}"
        )

        # Return response
        created_timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return CreateTimeOffPolicyResponse(
            id=str(new_policy.id),
            name=new_policy.name,
            typeId=str(new_policy.type_id),
            created=created_timestamp,
        )


__all__ = ["get_policies", "get_employee_policies", "assign_policy", "create_policy"]
