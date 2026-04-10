"""Time-off request management tools for BambooHR MCP server.

Implements:
- get_requests: Retrieve time-off requests with filtering
- create_request: Create new time-off requests with validation
- update_request_status: Approve/deny/cancel requests

Persona-based access control:
- HR Admin: Full access to all employees' requests
- Manager: Can view/create requests for direct reports
- Employee: Can view/create own requests only
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

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
from mcp_auth import get_current_user, require_roles, require_scopes, user_has_role
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.orm import joinedload

# ============================================================================
# Helper Functions
# ============================================================================


def _split_amount_by_year(
    start_date: date, end_date: date, total_amount: Decimal
) -> dict[int, Decimal]:
    """Split a time-off amount proportionally across years based on days in each year.

    For requests that span year boundaries, this calculates how much time should be
    deducted from each year's balance based on the number of days in that year.

    Args:
        start_date: Start date of the time-off request
        end_date: End date of the time-off request
        total_amount: Total amount of time off (in days or hours)

    Returns:
        Dictionary mapping year -> amount to deduct from that year's balance

    Example:
        Request from Dec 30, 2025 to Jan 2, 2026 with 4 days total:
        - 2 days in 2025 (Dec 30, 31)
        - 2 days in 2026 (Jan 1, 2)
        Returns: {2025: Decimal("2.00"), 2026: Decimal("2.00")}
    """
    from datetime import timedelta

    if start_date > end_date:
        raise ValueError("Start date must be <= end date")

    # Calculate total days in the request
    total_days = (end_date - start_date).days + 1

    if total_days == 0:
        return {}

    # Calculate amount per day
    amount_per_day = total_amount / Decimal(total_days)

    # Count days in each year
    year_days: dict[int, int] = {}
    current = start_date
    while current <= end_date:
        year = current.year
        year_days[year] = year_days.get(year, 0) + 1
        current += timedelta(days=1)

    # Calculate proportional amounts for each year
    year_amounts: dict[int, Decimal] = {}
    for year, days in year_days.items():
        year_amounts[year] = amount_per_day * Decimal(days)

    return year_amounts


async def _check_employee_access(session, employee_id: int, user: dict) -> tuple[bool, str | None]:
    """Check if user has permission to access employee's time-off data.

    Args:
        session: Database session
        employee_id: Employee ID to check access for
        user: Current user dict from get_current_user()

    Returns:
        Tuple of (has_access, error_message)
    """
    # HR Admin has full access (also returns True if auth disabled)
    if user_has_role("hr_admin"):
        return (True, None)

    user_employee_id = user.get("employeeId")

    # Check if accessing own data (requires employee role per BUILD_PLAN)
    if user_has_role("employee") and user_employee_id == employee_id:
        return (True, None)

    # Check if manager of this employee
    if user_has_role("manager") and user_employee_id:
        employee = await session.get(Employee, employee_id)
        if employee and employee.supervisor_id == user_employee_id:
            return (True, None)

    return (False, "Insufficient permissions to access this employee's time-off data")


# ============================================================================
# get_requests
# ============================================================================


@require_roles("hr_admin", "manager", "employee")
@require_scopes("read:time_off")
async def get_requests(
    start: str,
    end: str,
    employee_id: str | None = None,
    request_id: str | None = None,
    status: list[str] | None = None,
    type_id: str | None = None,
) -> list[dict]:
    """Get time-off requests within a date range."""
    user = get_current_user()
    user_employee_id = user.get("employeeId")

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    async with get_session() as session:
        # If specific employee_id requested, check access
        if employee_id:
            has_access, error_msg = await _check_employee_access(session, int(employee_id), user)
            if not has_access:
                raise ValueError(error_msg)
        # Base query
        stmt = (
            select(TimeOffRequest)
            .options(
                joinedload(TimeOffRequest.employee),
                joinedload(TimeOffRequest.time_off_type),
                joinedload(TimeOffRequest.policy),
            )
            .where(
                and_(
                    TimeOffRequest.start_date <= end_date,
                    TimeOffRequest.end_date >= start_date,
                )
            )
        )

        # Apply persona-based filtering if no specific employee_id
        # Composite roles are ADDITIVE: [manager, employee] sees both direct reports AND own
        # user_has_role returns True if auth disabled, skipping filtering
        if not employee_id:
            if not user_has_role("hr_admin"):
                visible_employee_ids: list[int] = []

                # Manager role: can see direct reports' requests
                if user_has_role("manager") and user_employee_id:
                    direct_reports_stmt = select(Employee.id).where(
                        Employee.supervisor_id == user_employee_id
                    )
                    direct_reports_result = await session.execute(direct_reports_stmt)
                    direct_report_ids = [row[0] for row in direct_reports_result.all()]
                    visible_employee_ids.extend(direct_report_ids)

                # Employee role: can see own requests
                if user_has_role("employee") and user_employee_id:
                    if user_employee_id not in visible_employee_ids:
                        visible_employee_ids.append(user_employee_id)

                if not visible_employee_ids:
                    # No valid roles/employeeId: deny access
                    raise ValueError(
                        "Insufficient permissions: employeeId required for non-admin users"
                    )

                stmt = stmt.where(TimeOffRequest.employee_id.in_(visible_employee_ids))

        # Apply filters
        if employee_id:
            stmt = stmt.where(TimeOffRequest.employee_id == int(employee_id))

        if request_id:
            stmt = stmt.where(TimeOffRequest.id == int(request_id))

        if status:
            stmt = stmt.where(TimeOffRequest.status.in_(status))

        if type_id:
            try:
                type_id_int = int(type_id)
            except ValueError:
                raise ValueError(f"Invalid type_id: '{type_id}' (must be a numeric ID)")
            stmt = stmt.where(TimeOffRequest.type_id == type_id_int)

        result = await session.execute(stmt)
        requests = result.unique().scalars().all()

        # Format response
        return [_format_request_response(req) for req in requests]


def _generate_dates_map(start_date: date, end_date: date, total_amount: Decimal) -> dict:
    """Generate a map of dates to daily hours.

    For multi-day requests, distributes hours evenly across days.
    For single-day requests, returns that day with the total amount.
    """
    from datetime import timedelta

    dates = {}
    current = start_date
    num_days = (end_date - start_date).days + 1

    if num_days <= 0:
        return {}

    # Calculate daily hours (distribute evenly)
    daily_hours = total_amount / Decimal(num_days)

    while current <= end_date:
        dates[current.isoformat()] = str(daily_hours)
        current += timedelta(days=1)

    return dates


def _format_request_response(request: TimeOffRequest) -> dict:
    """Format a TimeOffRequest model into BambooHR API format."""
    # Get user context for actions
    user = get_current_user()
    user_employee_id = user.get("employeeId") if user else None
    # user_has_role returns True if auth disabled, giving full admin access
    is_hr_admin = user_has_role("hr_admin")
    is_manager = user_has_role("manager")
    is_own_request = user_employee_id == request.employee_id

    # Determine supervisor relationship (request owner's supervisor)
    is_direct_report = (
        is_manager and user_employee_id and request.employee.supervisor_id == user_employee_id
    )

    # Request status checks
    is_requested = request.status == "requested"
    is_approved = request.status == "approved"
    is_before_start = request.start_date > date.today()

    # Generate actions based on user context and request status
    can_cancel = (is_own_request and is_requested) or (
        is_hr_admin and is_approved and is_before_start
    )
    actions = {
        "view": True,  # Always true if user can see the request
        "edit": is_own_request and is_requested,  # Can edit own pending requests
        "cancel": can_cancel,
        "approve": (is_hr_admin or is_direct_report) and is_requested,
        "deny": (is_hr_admin or is_direct_report) and is_requested,
        "bypass": is_hr_admin and is_requested,  # HR Admin can bypass approval
    }

    # Generate dates map
    dates = _generate_dates_map(request.start_date, request.end_date, request.amount)

    return {
        "id": str(request.id),
        "employeeId": str(request.employee_id),
        "name": f"{request.employee.first_name} {request.employee.last_name}",
        "status": {
            "status": request.status,
            "lastChanged": request.updated_at.date().isoformat(),
            "lastChangedByUserId": str(request.approver_id) if request.approver_id else None,
        },
        "start": request.start_date.isoformat(),
        "end": request.end_date.isoformat(),
        "created": request.created_at.date().isoformat(),
        "type": {
            "id": str(request.type_id),
            "name": request.time_off_type.name,
            "icon": "palm-trees",  # Default icon
        },
        "amount": {"unit": request.units, "amount": str(request.amount)},
        "actions": actions,
        "dates": dates,
        "notes": {"employee": request.notes or "", "manager": request.approval_notes or ""},
    }


# ============================================================================
# create_request
# ============================================================================


@require_roles("hr_admin", "manager", "employee")
@require_scopes("write:time_off")
async def create_request(
    employee_id: str,
    time_off_type_id: str,
    start: str,
    end: str,
    amount: str,
    notes: str | None = None,
    status: str = "requested",
) -> dict:
    """Create a new time-off request."""
    from decimal import Decimal

    from db import TimeOffBalance

    user = get_current_user()

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    # Validate status parameter (BUILD_PLAN validation rule #6)
    if status not in ("requested", "approved"):
        raise ValueError(f"Invalid status: '{status}'. Status must be 'requested' or 'approved'")

    # Only HR Admin can create pre-approved requests (BUILD_PLAN validation rule #6)
    if status == "approved" and not user_has_role("hr_admin"):
        raise ValueError("Only HR Admin users can create pre-approved requests")

    # Validate date range
    if end_date < start_date:
        raise ValueError("End date must be >= start date")

    # Validate dates are not in the past for non-admin users
    # BUILD_PLAN: "Past dates → Allow if HR Admin, reject for Employee"
    # BUILD_PLAN validation rule #4: "both in future or today"
    today = date.today()
    if start_date < today and not user_has_role("hr_admin"):
        raise ValueError("Cannot create time-off requests with past dates")

    # Validate amount is positive
    try:
        amount_decimal = Decimal(amount)
        if amount_decimal <= 0:
            raise ValueError("Amount must be greater than zero")
    except (ValueError, InvalidOperation) as e:
        if "greater than zero" in str(e):
            raise
        raise ValueError(f"Invalid amount format: {amount}")

    async with get_session() as session:
        # 0. Check permissions
        has_access, error_msg = await _check_employee_access(session, int(employee_id), user)
        if not has_access:
            raise ValueError(error_msg)

        # 1. Verify employee exists and is active
        employee = await session.get(Employee, int(employee_id))
        if not employee:
            raise ValueError("Employee not found")
        if employee.status != "Active":
            raise ValueError("Employee is not active")

        # 2. Lookup time-off type and validate it exists
        time_off_type = await session.get(TimeOffType, int(time_off_type_id))
        if not time_off_type:
            raise ValueError("Invalid time-off type ID")

        # 3. Get employee's assigned policy via EmployeePolicy
        # Use the same robust pattern as time_off_balances.py to handle multiple policies
        # Query active policies for this employee and time-off type, ordered by most recent
        policy_stmt = (
            select(EmployeePolicy, TimeOffPolicy)
            .join(TimeOffPolicy, EmployeePolicy.policy_id == TimeOffPolicy.id)
            .where(
                and_(
                    EmployeePolicy.employee_id == int(employee_id),
                    TimeOffPolicy.type_id == int(time_off_type_id),
                    EmployeePolicy.effective_date <= start_date,
                    (EmployeePolicy.end_date.is_(None)) | (EmployeePolicy.end_date >= start_date),
                )
            )
            .order_by(EmployeePolicy.effective_date.desc())
        )
        policy_result = await session.execute(policy_stmt)
        policy_row = policy_result.first()

        if not policy_row:
            raise ValueError("Employee not assigned to this time-off policy")

        emp_policy, policy = policy_row

        # 4. Split amount across years if request spans year boundary
        year_amounts = _split_amount_by_year(start_date, end_date, amount_decimal)

        # 5. Get and lock all affected balance records
        # Use row-level locking to prevent concurrent updates from overwriting each other
        balances: dict[int, TimeOffBalance] = {}
        for year in year_amounts.keys():
            balance_stmt = (
                select(TimeOffBalance)
                .where(
                    and_(
                        TimeOffBalance.employee_id == int(employee_id),
                        TimeOffBalance.policy_id == policy.id,
                        TimeOffBalance.year == year,
                    )
                )
                .with_for_update()  # Lock row for concurrent update safety
            )
            balance_result = await session.execute(balance_stmt)
            balance = balance_result.scalar_one_or_none()

            if not balance:
                raise ValueError(f"Employee not assigned to this time-off policy for year {year}")

            balances[year] = balance

        # 6. Check available balance for each year and warn if any year is insufficient
        # Handle potential NULL values from database (columns lack nullable=False constraints)
        warning = None
        for year, year_amount in year_amounts.items():
            balance = balances[year]
            current_balance = balance.balance or Decimal("0.00")
            current_scheduled = balance.scheduled or Decimal("0.00")
            available = current_balance - current_scheduled

            if available < year_amount:
                if warning:
                    warning += f"; Year {year}: available {available}, need {year_amount}"
                else:
                    warning = (
                        f"Insufficient balance for year {year}. "
                        f"Available: {available}, Requested: {year_amount}"
                    )

        # 7. Create request
        new_request = TimeOffRequest(
            employee_id=int(employee_id),
            type_id=int(time_off_type_id),
            policy_id=policy.id,
            start_date=start_date,
            end_date=end_date,
            amount=amount_decimal,
            units=time_off_type.units,
            status=status,
            notes=notes,
        )

        session.add(new_request)
        await session.flush()

        # 8. Update balances for each affected year based on status
        # Handle potential NULL values from database (columns lack nullable=False constraints)
        for year, year_amount in year_amounts.items():
            balance = balances[year]
            if status == "approved":
                # Approved: Deduct from balance and move to used
                balance.balance = (balance.balance or Decimal("0.00")) - year_amount
                balance.used = (balance.used or Decimal("0.00")) + year_amount
            elif status == "requested":
                # Pending: Add to scheduled (reserves the time)
                balance.scheduled = (balance.scheduled or Decimal("0.00")) + year_amount

        await session.commit()

        # Return response
        response = {
            "id": str(new_request.id),
            "created": new_request.created_at.isoformat(),
        }

        if warning:
            response["warning"] = warning

        return response


# ============================================================================
# update_request_status
# ============================================================================


class UpdateRequestStatusInput(BaseModel):
    """Input model for update_request_status."""

    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(
        ..., alias="requestId", description="ID of the time-off request to update"
    )
    status: Literal["approved", "denied", "canceled", "requested"] = Field(
        ..., description="New status for the request"
    )
    note: str | None = Field(None, description="Optional note for status change")


class UpdateRequestStatusOutput(BaseModel):
    """Output model for update_request_status."""

    model_config = ConfigDict(populate_by_name=True)

    success: bool = Field(..., description="Whether the update was successful")
    previous_status: str = Field(
        ..., alias="previousStatus", description="Previous status of the request"
    )
    new_status: str = Field(..., alias="newStatus", description="New status of the request")


@require_roles("hr_admin", "manager", "employee")
@require_scopes("write:time_off")
async def update_request_status(
    request: UpdateRequestStatusInput,
) -> UpdateRequestStatusOutput:
    """Update the status of a time-off request."""
    user = get_current_user()
    user_employee_id = user.get("employeeId")
    # user_has_role returns True if auth disabled, giving full admin access
    is_hr_admin = user_has_role("hr_admin")
    is_manager = user_has_role("manager")
    is_employee = user_has_role("employee")

    async with get_session() as session:
        # 1. Get the request with row-level locking to prevent race conditions
        request_stmt = (
            select(TimeOffRequest)
            .where(TimeOffRequest.id == int(request.request_id))
            .with_for_update()  # Lock to prevent concurrent status updates
        )
        request_result = await session.execute(request_stmt)
        time_off_request = request_result.scalar_one_or_none()

        if not time_off_request:
            raise ValueError("Time-off request not found")

        # 2. Get employee to check supervisor relationship
        employee_stmt = select(Employee).where(Employee.id == time_off_request.employee_id)
        employee_result = await session.execute(employee_stmt)
        employee = employee_result.scalar_one()

        # 3. Check permissions
        # Check higher privilege roles first to handle composite roles correctly
        is_own_request = user_employee_id == time_off_request.employee_id
        is_direct_report = (
            is_manager and user_employee_id and employee.supervisor_id == user_employee_id
        )

        if is_hr_admin:
            # HR Admin has full access
            pass
        elif is_manager and is_direct_report:
            # Managers can approve/deny direct reports only (not cancel)
            if request.status not in ["approved", "denied"]:
                raise PermissionError("Managers can only approve or deny direct reports' requests")
        elif is_employee and is_own_request and request.status == "canceled":
            # Employees can only cancel their own requests
            pass
        else:
            # No permission
            raise PermissionError("Cannot update this time-off request")

        # 4. Validate status transition
        previous_status = time_off_request.status
        new_status = request.status

        # Define valid transitions
        valid_transitions = {
            TimeOffRequestStatus.REQUESTED.value: [
                "approved",
                "denied",
                "canceled",
            ],
            TimeOffRequestStatus.APPROVED.value: ["canceled"],
            TimeOffRequestStatus.DENIED.value: [],  # Final state
            TimeOffRequestStatus.CANCELED.value: [],  # Final state (except HR Admin can re-open)
        }

        # Check transition validity
        if new_status not in valid_transitions.get(previous_status, []):
            # Special case: HR Admin can re-open canceled requests to any status
            if not (is_hr_admin and previous_status == TimeOffRequestStatus.CANCELED.value):
                raise ValueError(f"Cannot change status from {previous_status} to {new_status}")

        # 5. Additional validation for canceling approved requests
        if previous_status == TimeOffRequestStatus.APPROVED.value and new_status == "canceled":
            # Allow cancel before start date, or HR Admin anytime
            if not is_hr_admin and time_off_request.start_date <= date.today():
                raise ValueError("Cannot cancel approved request after start date")

        # 6. Split amount across years if request spans year boundary
        # NOTE: This assumes the request was created with the year-spanning logic.
        # For requests created before this fix (which only updated start_date.year),
        # this will cause incorrect balance updates. Since this is a new system,
        # existing year-spanning requests should be minimal. If data migration is needed,
        # check if scheduled/used in end_date.year is 0 to detect old-format requests.
        year_amounts = _split_amount_by_year(
            time_off_request.start_date, time_off_request.end_date, time_off_request.amount
        )

        # 7. Get and lock all affected balance records
        # Use row-level locking to prevent concurrent updates from overwriting each other
        balances: dict[int, TimeOffBalance] = {}
        for year in year_amounts.keys():
            balance_stmt = (
                select(TimeOffBalance)
                .where(
                    TimeOffBalance.employee_id == time_off_request.employee_id,
                    TimeOffBalance.policy_id == time_off_request.policy_id,
                    TimeOffBalance.year == year,
                )
                .with_for_update()  # Lock for concurrent update safety
            )
            balance_result = await session.execute(balance_stmt)
            balance = balance_result.scalar_one_or_none()

            # Balance record is required for all status changes
            if balance is None:
                raise ValueError(
                    f"Balance record not found for this employee and policy for year {year}"
                )

            balances[year] = balance

        # 8. Update balances for each affected year based on status change
        for year, year_amount in year_amounts.items():
            balance = balances[year]

            # Handle NULL values from database
            current_balance = balance.balance or Decimal("0.00")
            current_used = balance.used or Decimal("0.00")
            current_scheduled = balance.scheduled or Decimal("0.00")

            if new_status == "approved":
                # Approve: Deduct from balance, add to used
                # If coming from requested, also remove from scheduled
                # If coming from canceled, just deduct from balance and add to used
                if previous_status == TimeOffRequestStatus.REQUESTED.value:
                    balance.scheduled = current_scheduled - year_amount
                balance.balance = current_balance - year_amount
                balance.used = current_used + year_amount
            elif new_status == "denied":
                # Deny: Remove from scheduled if was requested
                if previous_status == TimeOffRequestStatus.REQUESTED.value:
                    balance.scheduled = current_scheduled - year_amount
            elif new_status == "canceled":
                if previous_status == TimeOffRequestStatus.APPROVED.value:
                    # Cancel approved: Restore balance
                    balance.balance = current_balance + year_amount
                    balance.used = current_used - year_amount
                elif previous_status == TimeOffRequestStatus.REQUESTED.value:
                    # Cancel requested: Remove from scheduled
                    balance.scheduled = current_scheduled - year_amount
            elif new_status == "requested":
                # Re-opening a canceled request: Add back to scheduled
                # This can only happen when HR Admin re-opens a canceled request
                if previous_status == TimeOffRequestStatus.CANCELED.value:
                    balance.scheduled = current_scheduled + year_amount

        # 9. Update request status and approval metadata
        time_off_request.status = new_status

        # Record approval/denial/cancellation metadata
        if new_status in ["approved", "denied", "canceled"]:
            time_off_request.approver_id = user_employee_id
            time_off_request.approved_at = datetime.now(UTC)
            if request.note:
                time_off_request.approval_notes = request.note
        elif new_status == "requested":
            # Clear approval metadata when re-opening to requested status
            time_off_request.approver_id = None
            time_off_request.approved_at = None
            time_off_request.approval_notes = None

        # Ensure changes are tracked
        for balance in balances.values():
            session.add(balance)
        session.add(time_off_request)

        await session.commit()

        return UpdateRequestStatusOutput(
            success=True,
            previous_status=previous_status,
            new_status=new_status,
        )


__all__ = ["get_requests", "create_request", "update_request_status"]
