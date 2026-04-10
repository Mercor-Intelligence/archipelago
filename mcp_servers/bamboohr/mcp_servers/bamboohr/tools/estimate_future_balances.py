"""Estimate future balances tool for BambooHR MCP server.

Implements:
- estimate_future_balances: Projects future time-off balances (#60)

BambooHR API: GET /v1/employees/{employeeId}/time_off/estimate?date={futureDate}

Persona-based access control:
- HR Admin: Any employee
- Manager: Direct reports only
- Employee: Self only
"""

from datetime import date as date_type
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

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
from mcp_auth import require_roles, require_scopes, user_has_role, user_has_scope
from schemas import EstimateFutureBalanceEntry
from sqlalchemy import and_, select

from .auth_helpers import get_user_context


@require_roles("hr_admin", "manager", "employee")
@require_scopes("read:time_off")
async def estimate_future_balances(
    employeeId: str,  # noqa: N803
    date: str,  # noqa: A002
) -> dict:
    """Project future time-off balances based on accrual schedules and pending requests."""
    # Validate employee ID format
    try:
        emp_id = int(employeeId)
    except (ValueError, TypeError):
        return {"error": {"code": 400, "message": "Invalid employee ID format"}}

    # Check scope permissions (user_has_scope returns True if auth disabled)
    user_employee_id, _ = get_user_context()
    is_self = user_employee_id == emp_id

    # Must have read:time_off scope, or read:time_off:self if viewing own balances
    has_full_scope = user_has_scope("read:time_off")
    has_self_scope = user_has_scope("read:time_off:self")

    if not has_full_scope and not (has_self_scope and is_self):
        return {
            "error": {
                "code": 403,
                "message": "Access denied: Missing scope(s): read:time_off",
            }
        }

    # Parse and validate date
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return {"error": {"code": 400, "message": "Invalid date format. Expected YYYY-MM-DD"}}

    today = date_type.today()
    if target_date < today:
        return {"error": {"code": 422, "message": "Date must be in the future (>= today)"}}

    current_year = today.year

    async with get_session() as session:
        # Verify employee exists and get their info
        emp_result = await session.execute(select(Employee).where(Employee.id == emp_id))
        employee = emp_result.scalar_one_or_none()

        if not employee:
            return {"error": {"code": 404, "message": "Employee not found"}}

        # Check persona permissions
        # user_has_role returns True if auth disabled, giving full admin access
        is_hr_admin = user_has_role("hr_admin")

        # Check if manager viewing direct report
        is_manager_of = False
        if user_has_role("manager") and user_employee_id:
            is_manager_of = employee.supervisor_id == user_employee_id

        if not (is_hr_admin or is_self or is_manager_of):
            return {
                "error": {
                    "code": 403,
                    "message": "Insufficient permissions to view estimates",
                }
            }

        # Get employee's active policy assignments
        policies_result = await session.execute(
            select(
                EmployeePolicy,
                TimeOffPolicy,
                TimeOffType,
            )
            .join(TimeOffPolicy, EmployeePolicy.policy_id == TimeOffPolicy.id)
            .join(TimeOffType, TimeOffPolicy.type_id == TimeOffType.id)
            .where(
                EmployeePolicy.employee_id == emp_id,
                EmployeePolicy.effective_date <= today,
                (EmployeePolicy.end_date.is_(None) | (EmployeePolicy.end_date >= today)),
            )
            .order_by(EmployeePolicy.effective_date.desc())
        )
        policy_rows = policies_result.all()

        if not policy_rows:
            return {"estimates": []}

        # Deduplicate by time-off type
        seen_type_ids: set[int] = set()
        unique_policy_rows = []
        for row in policy_rows:
            time_off_type = row[2]
            if time_off_type.id not in seen_type_ids:
                seen_type_ids.add(time_off_type.id)
                unique_policy_rows.append(row)

        # Get pending requests between now and target date
        pending_result = await session.execute(
            select(TimeOffRequest).where(
                and_(
                    TimeOffRequest.employee_id == emp_id,
                    TimeOffRequest.status == TimeOffRequestStatus.REQUESTED.value,
                    TimeOffRequest.start_date <= target_date,
                    TimeOffRequest.end_date >= today,
                )
            )
        )
        pending_requests = pending_result.scalars().all()

        # Group pending amounts by type_id
        pending_by_type: dict[int, Decimal] = {}
        for req in pending_requests:
            current = pending_by_type.get(req.type_id, Decimal("0"))
            pending_by_type[req.type_id] = current + req.amount

        estimates = []
        for emp_policy, policy, time_off_type in unique_policy_rows:
            # Get current balance
            balance_result = await session.execute(
                select(TimeOffBalance).where(
                    TimeOffBalance.employee_id == emp_id,
                    TimeOffBalance.policy_id == policy.id,
                    TimeOffBalance.year == current_year,
                )
            )
            balance_record = balance_result.scalar_one_or_none()

            if balance_record:
                # Available balance = balance - scheduled (approved but not yet taken)
                balance = balance_record.balance or Decimal("0.00")
                scheduled = balance_record.scheduled or Decimal("0.00")
                current_balance = balance - scheduled
            else:
                current_balance = Decimal("0.00")

            # Calculate projected accrual
            projected_accrual = _calculate_accrual(
                policy=policy,
                from_date=today,
                to_date=target_date,
                current_balance=current_balance,
            )

            # Get pending requests for this type
            pending_amount = pending_by_type.get(time_off_type.id, Decimal("0.00"))

            # Calculate estimated balance
            estimated_balance = current_balance + projected_accrual - pending_amount

            # Apply max balance cap if set
            if policy.max_balance is not None:
                estimated_balance = min(estimated_balance, policy.max_balance)

            # Format as strings with 2 decimal places
            two_dp = Decimal("0.01")
            entry = EstimateFutureBalanceEntry(
                timeOffTypeId=str(time_off_type.id),
                timeOffTypeName=time_off_type.name,
                estimatedBalance=str(estimated_balance.quantize(two_dp, rounding=ROUND_HALF_UP)),
                currentBalance=str(current_balance.quantize(two_dp, rounding=ROUND_HALF_UP)),
                projectedAccrual=str(projected_accrual.quantize(two_dp, rounding=ROUND_HALF_UP)),
                pendingRequests=str(pending_amount.quantize(two_dp, rounding=ROUND_HALF_UP)),
                asOfDate=target_date.isoformat(),
            )
            estimates.append(entry.model_dump(by_alias=True))

        logger.info(f"Estimated {len(estimates)} balances for employee {emp_id}")
        return {"estimates": estimates}


def _calculate_accrual(
    policy: TimeOffPolicy,
    from_date: date_type,
    to_date: date_type,
    current_balance: Decimal,
) -> Decimal:
    """Calculate projected accrual based on policy settings."""
    # Manual policies don't accrue automatically
    if policy.accrual_type == "manual" or not policy.accrual_rate:
        return Decimal("0.00")

    days_until_target = (to_date - from_date).days
    if days_until_target <= 0:
        return Decimal("0.00")

    accrual_rate = policy.accrual_rate

    # Calculate based on frequency
    if policy.accrual_frequency == "monthly":
        # Approximate months (30 days per month)
        months_elapsed = Decimal(str(days_until_target)) / Decimal("30")
        projected_accrual = months_elapsed * accrual_rate
    elif policy.accrual_frequency == "per_pay_period":
        # Assume biweekly (14 days per pay period)
        pay_periods = Decimal(str(days_until_target)) / Decimal("14")
        projected_accrual = pay_periods * accrual_rate
    elif policy.accrual_frequency == "annual":
        # Annual accrual prorated by days
        years_elapsed = Decimal(str(days_until_target)) / Decimal("365")
        projected_accrual = years_elapsed * accrual_rate
    elif policy.accrual_frequency == "hourly":
        # Per BUILD_PLAN: assume 8-hour workday, 5 days/week
        # This is a simplification - actual implementation would need work hours tracking
        work_days = Decimal(str(days_until_target)) * Decimal("5") / Decimal("7")
        work_hours = work_days * Decimal("8")
        projected_accrual = work_hours * accrual_rate
    else:
        # Unknown frequency - no accrual
        projected_accrual = Decimal("0.00")

    # Apply max balance cap (can't accrue beyond max)
    if policy.max_balance is not None:
        max_accrual = policy.max_balance - current_balance
        if max_accrual < Decimal("0"):
            max_accrual = Decimal("0")
        projected_accrual = min(projected_accrual, max_accrual)

    return projected_accrual


__all__ = ["estimate_future_balances"]
