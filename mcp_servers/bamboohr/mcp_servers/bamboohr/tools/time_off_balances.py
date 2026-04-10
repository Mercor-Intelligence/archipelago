"""Time-off balance tools for BambooHR MCP server.

Implements:
- get_balances: Retrieve employee time-off balances (#57)
- update_balance: Manually adjust balances with audit trail (#58)

Persona-based access control:
- HR Admin: Full access to all employees and adjustments
- Manager: Can view direct reports' balances
- Employee: Can view own balances only
"""

from datetime import date, datetime
from decimal import Decimal

from db import (
    BalanceAdjustment,
    Employee,
    EmployeePolicy,
    EmployeeStatus,
    TimeOffBalance,
    TimeOffPolicy,
    TimeOffType,
    get_session,
)
from loguru import logger
from mcp_auth import require_roles, require_scopes, user_has_role, user_has_scope
from schemas import BalanceAdjustmentResponse, BalanceEntry
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth_helpers import get_user_context


@require_roles("hr_admin", "manager", "employee")
@require_scopes("read:time_off")
async def get_balances(employeeId: str) -> dict:  # noqa: N803
    """Get time-off balances for an employee."""
    try:
        emp_id = int(employeeId)
    except (ValueError, TypeError):
        return {"error": {"code": 400, "message": "Invalid employee ID format"}}
    current_year = date.today().year

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

    async with get_session() as session:
        # Verify employee exists and get their info
        emp_result = await session.execute(select(Employee).where(Employee.id == emp_id))
        employee = emp_result.scalar_one_or_none()

        if not employee:
            return {"error": {"code": 404, "message": "Employee not found"}}

        # user_has_role returns True if auth disabled, giving full admin access
        is_hr_admin = user_has_role("hr_admin")

        # Check if manager viewing direct report
        is_manager_of = False
        if user_has_role("manager") and user_employee_id:
            # Check if employee's supervisor is the current user
            is_manager_of = employee.supervisor_id == user_employee_id

        if not (is_hr_admin or is_self or is_manager_of):
            return {
                "error": {
                    "code": 403,
                    "message": "Insufficient permissions to view balances",
                }
            }

        # Get employee's active policy assignments
        # Active = effective_date <= today AND (end_date is null OR end_date >= today)
        # Order by effective_date DESC so we can pick the most recent per time-off type
        today = date.today()
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
                EmployeePolicy.effective_date <= today,  # Exclude future-dated assignments
                (EmployeePolicy.end_date.is_(None) | (EmployeePolicy.end_date >= today)),
            )
            .order_by(EmployeePolicy.effective_date.desc())
        )
        policy_rows = policies_result.all()

        if not policy_rows:
            # No policies assigned - return empty balances array
            return {"balances": []}

        # Deduplicate by time-off type, keeping only the most recent policy per type
        # (sorted by effective_date DESC, so first occurrence wins)
        seen_type_ids: set[int] = set()
        unique_policy_rows = []
        for row in policy_rows:
            time_off_type = row[2]  # TimeOffType from the tuple
            if time_off_type.id not in seen_type_ids:
                seen_type_ids.add(time_off_type.id)
                unique_policy_rows.append(row)

        balances = []
        for emp_policy, policy, time_off_type in unique_policy_rows:
            # Cache policy values before any potential rollback to avoid expired object access
            policy_id = policy.id
            policy_name = policy.name
            policy_carry_over = policy.carry_over
            policy_carry_over_max = policy.carry_over_max
            type_id = time_off_type.id
            type_name = time_off_type.name
            type_units = time_off_type.units
            effective_date = emp_policy.effective_date

            # Get current balance for this policy
            balance_result = await session.execute(
                select(TimeOffBalance).where(
                    TimeOffBalance.employee_id == emp_id,
                    TimeOffBalance.policy_id == policy_id,
                    TimeOffBalance.year == current_year,
                )
            )
            balance_record = balance_result.scalar_one_or_none()

            # Auto-create balance record for current year if it doesn't exist
            # This handles the case where policies were assigned in previous years
            # but balance records weren't created for the current year
            if not balance_record:
                logger.info(
                    f"Auto-creating balance record for employee {emp_id}, "
                    f"policy {policy_id}, year {current_year}"
                )
                try:
                    # Use savepoint to avoid rolling back the entire transaction
                    async with session.begin_nested():
                        balance_record = TimeOffBalance(
                            employee_id=emp_id,
                            policy_id=policy_id,
                            year=current_year,
                            balance=Decimal("0.00"),
                            used=Decimal("0.00"),
                            scheduled=Decimal("0.00"),
                        )
                        session.add(balance_record)
                        await session.flush()
                except IntegrityError:
                    # Handle race condition: another request created this record concurrently
                    # Savepoint already rolled back, just re-fetch the existing record
                    balance_result = await session.execute(
                        select(TimeOffBalance).where(
                            TimeOffBalance.employee_id == emp_id,
                            TimeOffBalance.policy_id == policy_id,
                            TimeOffBalance.year == current_year,
                        )
                    )
                    balance_record = balance_result.scalar_one()

            # Calculate values (handle potential NULL values from database)
            current_balance = balance_record.balance or Decimal("0.00")
            used = balance_record.used or Decimal("0.00")
            scheduled = balance_record.scheduled or Decimal("0.00")

            # Get carry-over from previous year if policy allows
            carry_over = Decimal("0.00")
            if policy_carry_over:
                prev_year_result = await session.execute(
                    select(TimeOffBalance).where(
                        TimeOffBalance.employee_id == emp_id,
                        TimeOffBalance.policy_id == policy_id,
                        TimeOffBalance.year == current_year - 1,
                    )
                )
                prev_balance = prev_year_result.scalar_one_or_none()
                if prev_balance:
                    # Apply carry-over limit if set, clamping negative values to 0
                    # Include scheduled time off in the calculation
                    # Handle potential NULL values from database
                    prev_bal = prev_balance.balance or Decimal("0.00")
                    prev_used = prev_balance.used or Decimal("0.00")
                    prev_scheduled = prev_balance.scheduled or Decimal("0.00")
                    remaining = prev_bal - prev_used - prev_scheduled
                    # Use 'is not None' to correctly handle carry_over_max of 0
                    if policy_carry_over_max is not None:
                        # Clamp to [0, carry_over_max] - can't carry over negative
                        carry_over = max(Decimal("0.00"), min(remaining, policy_carry_over_max))
                    else:
                        carry_over = max(remaining, Decimal("0.00"))

            # Calculate accrued (current balance + used represents total accrued this year)
            accrued = current_balance + used

            # Calculate available balance per spec: balance = accrued + carryOver - used
            # Since current_balance = accrued - used (already net), we add carry_over
            available_balance = current_balance + carry_over

            # Build response using Pydantic schema
            entry = BalanceEntry(
                timeOffTypeId=str(type_id),
                timeOffTypeName=type_name,
                policyId=policy_id,
                policyName=policy_name,
                balance=str(available_balance),
                used=str(used),
                scheduled=str(scheduled),
                accrued=str(accrued),
                carryOver=str(carry_over),
                unit=type_units,
                effectiveDate=effective_date.isoformat(),
            )
            balances.append(entry.model_dump(by_alias=True))

        # Commit any auto-created balance records
        await session.commit()

        logger.info(f"Retrieved {len(balances)} balances for employee {emp_id}")
        return {"balances": balances}


@require_roles("hr_admin")
@require_scopes("write:time_off")
async def update_balance(
    employeeId: str,  # noqa: N803
    timeOffTypeId: int,  # noqa: N803
    amount: float,
    note: str,
    date: str | None = None,  # noqa: A002
) -> dict:
    """Manually adjust an employee's time-off balance."""
    adjusted_by_employee_id, _ = get_user_context()
    try:
        emp_id = int(employeeId)
    except (ValueError, TypeError):
        return {"error": {"code": 400, "message": "Invalid employee ID format"}}
    # Use 4 decimal precision for adjustment amount to match BalanceAdjustment audit table
    # (Numeric(10, 4)) and BUILD_PLAN examples (e.g., -6.0000)
    adjustment_amount = Decimal(str(amount)).quantize(Decimal("0.0001"))
    # Parse date with error handling
    if date:
        try:
            adjustment_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return {"error": {"code": 400, "message": "Invalid date format. Expected YYYY-MM-DD"}}
    else:
        # Use datetime.today().date() since 'date' parameter shadows the type
        adjustment_date = datetime.today().date()
    current_year = adjustment_date.year

    # Validate note is provided
    if not note or not note.strip():
        return {"error": {"code": 400, "message": "Note is required"}}

    # Validate amount is not zero
    if adjustment_amount == 0:
        return {"error": {"code": 422, "message": "Adjustment amount cannot be zero"}}

    async with get_session() as session:
        # Verify employee exists
        emp_result = await session.execute(select(Employee).where(Employee.id == emp_id))
        employee = emp_result.scalar_one_or_none()

        if not employee:
            return {"error": {"code": 404, "message": "Employee not found"}}

        # Per BUILD_PLAN: "Verify employee exists and is active"
        if employee.status != EmployeeStatus.ACTIVE.value:
            return {
                "error": {
                    "code": 422,
                    "message": "Cannot adjust balance for inactive employee",
                }
            }

        # Verify time-off type exists
        type_result = await session.execute(
            select(TimeOffType).where(TimeOffType.id == timeOffTypeId)
        )
        time_off_type = type_result.scalar_one_or_none()

        if not time_off_type:
            return {"error": {"code": 422, "message": "Invalid time-off type"}}

        # Find employee's policy for this time-off type that was active on adjustment_date
        # For backdated adjustments, use adjustment_date; for current adjustments, use today
        # Active = effective_date <= target_date AND (end_date is null OR end_date > target_date)
        # Order by effective_date DESC to get the most recent policy if multiple match
        policy_result = await session.execute(
            select(EmployeePolicy, TimeOffPolicy)
            .join(TimeOffPolicy, EmployeePolicy.policy_id == TimeOffPolicy.id)
            .where(
                EmployeePolicy.employee_id == emp_id,
                TimeOffPolicy.type_id == timeOffTypeId,
                EmployeePolicy.effective_date <= adjustment_date,
                (EmployeePolicy.end_date.is_(None) | (EmployeePolicy.end_date >= adjustment_date)),
            )
            .order_by(EmployeePolicy.effective_date.desc())
        )
        policy_row = policy_result.first()

        if not policy_row:
            return {
                "error": {
                    "code": 422,
                    "message": "Employee not assigned to this time-off policy",
                }
            }

        _, policy = policy_row

        # Get or create current balance record with row-level lock to prevent
        # concurrent updates from overwriting each other
        balance_result = await session.execute(
            select(TimeOffBalance)
            .where(
                TimeOffBalance.employee_id == emp_id,
                TimeOffBalance.policy_id == policy.id,
                TimeOffBalance.year == current_year,
            )
            .with_for_update()  # Lock row for concurrent update safety
        )
        balance_record = balance_result.scalar_one_or_none()

        if not balance_record:
            # Create balance record if it doesn't exist
            # Handle race condition where concurrent requests may try to insert
            try:
                balance_record = TimeOffBalance(
                    employee_id=emp_id,
                    policy_id=policy.id,
                    year=current_year,
                    balance=Decimal("0.00"),
                    used=Decimal("0.00"),
                    scheduled=Decimal("0.00"),
                )
                session.add(balance_record)
                await session.flush()
            except IntegrityError:
                # Another concurrent request created the record - rollback and re-fetch
                await session.rollback()
                balance_result = await session.execute(
                    select(TimeOffBalance)
                    .where(
                        TimeOffBalance.employee_id == emp_id,
                        TimeOffBalance.policy_id == policy.id,
                        TimeOffBalance.year == current_year,
                    )
                    .with_for_update()
                )
                balance_record = balance_result.scalar_one()
            else:
                # Re-select with lock after successful insert to ensure consistency
                balance_result = await session.execute(
                    select(TimeOffBalance)
                    .where(TimeOffBalance.id == balance_record.id)
                    .with_for_update()
                )
                balance_record = balance_result.scalar_one()

        # Calculate new balance
        # Handle potential NULL value from database
        # Use 4 decimals for audit trail (BalanceAdjustment uses Numeric(10, 4))
        previous_balance = balance_record.balance or Decimal("0.00")
        # Extend previous_balance to 4 decimals for audit record
        previous_balance_4dp = previous_balance.quantize(Decimal("0.0001"))
        new_balance_4dp = previous_balance_4dp + adjustment_amount
        # Round to 2 decimals for actual TimeOffBalance table (Numeric(10, 2))
        new_balance = new_balance_4dp.quantize(Decimal("0.01"))

        # Create adjustment record for audit trail (uses 4 decimal precision)
        adjustment = BalanceAdjustment(
            employee_id=emp_id,
            policy_id=policy.id,
            adjustment_date=adjustment_date,
            amount=adjustment_amount,
            previous_balance=previous_balance_4dp,
            new_balance=new_balance_4dp,
            note=note.strip(),
            adjusted_by_id=adjusted_by_employee_id,
        )
        session.add(adjustment)

        # Update balance
        balance_record.balance = new_balance

        await session.commit()
        await session.refresh(adjustment)

        logger.info(
            f"Balance adjustment {adjustment.id}: employee {emp_id}, "
            f"policy {policy.id}, amount {adjustment_amount}, "
            f"previous {previous_balance} -> new {new_balance}"
        )

        # Build response using Pydantic schema (4 decimal precision per BUILD_PLAN)
        response = BalanceAdjustmentResponse(
            adjustmentId=str(adjustment.id),
            newBalance=float(new_balance_4dp),
            previousBalance=float(previous_balance_4dp),
            created=adjustment.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            warning="Balance will be negative" if new_balance_4dp < 0 else None,
        )

        return response.model_dump(by_alias=True, exclude_none=True)


__all__ = ["get_balances", "update_balance"]
