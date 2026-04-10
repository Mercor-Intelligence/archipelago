"""Who's Out tool for BambooHR MCP server.

Implements:
- get_whos_out: Shows employees who are out on time-off within a date range (#59)

BambooHR API: GET /v1/time_off/whos_out/?start={date}&end={date}

This is a public endpoint - any authenticated user can view who's out.
"""

from datetime import datetime

from db import (
    Employee,
    TimeOffRequest,
    TimeOffRequestStatus,
    TimeOffType,
    get_session,
)
from loguru import logger
from mcp_auth import require_roles, require_scopes, user_has_scope
from sqlalchemy import and_, select


@require_roles("hr_admin", "manager", "employee")
@require_scopes("read:time_off")
async def get_whos_out(start: str, end: str) -> dict:
    """Get employees who are out on time-off within a date range."""
    # Check scope permissions - any time_off scope allows viewing who's out
    # user_has_scope returns True if auth disabled, granting full access
    if not user_has_scope("read:time_off") and not user_has_scope("read:time_off:self"):
        return {
            "error": {
                "code": 403,
                "message": "Access denied: Missing scope(s): read:time_off",
            }
        }

    # Parse and validate dates
    try:
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
    except ValueError:
        return {"error": {"code": 400, "message": "Invalid start date format. Expected YYYY-MM-DD"}}

    try:
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        return {"error": {"code": 400, "message": "Invalid end date format. Expected YYYY-MM-DD"}}

    # Validate date range
    if end_date < start_date:
        return {"error": {"code": 400, "message": "End date must be on or after start date"}}

    async with get_session() as session:
        # Query approved time-off requests that overlap with the date range
        # Overlap: request.start <= query.end AND request.end >= query.start
        result = await session.execute(
            select(TimeOffRequest, Employee, TimeOffType)
            .join(Employee, TimeOffRequest.employee_id == Employee.id)
            .join(TimeOffType, TimeOffRequest.type_id == TimeOffType.id)
            .where(
                and_(
                    TimeOffRequest.status == TimeOffRequestStatus.APPROVED.value,
                    TimeOffRequest.start_date <= end_date,
                    TimeOffRequest.end_date >= start_date,
                )
            )
            .order_by(TimeOffRequest.start_date, Employee.last_name)
        )
        rows = result.all()

        entries = []
        for request, employee, time_off_type in rows:
            # Use display_name if available, otherwise construct from first/last name
            employee_name = (
                employee.display_name
                if employee.display_name
                else f"{employee.first_name} {employee.last_name}"
            )

            entries.append(
                {
                    "employeeId": str(employee.id),
                    "employeeName": employee_name,
                    "start": request.start_date.isoformat(),
                    "end": request.end_date.isoformat(),
                    "typeName": time_off_type.name,
                }
            )

        logger.info(f"Retrieved {len(entries)} who's out entries for {start} to {end}")
        return {"entries": entries}


__all__ = ["get_whos_out"]
