"""Authentication helpers for BambooHR tools.

Provides BambooHR-specific user context extraction from the shared mcp_auth module.
"""

from mcp_auth import get_current_user, user_has_role


def get_user_context() -> tuple[int | None, str]:
    """Get current user context from middleware.

    Returns:
        Tuple of (employee_id, persona)
        persona is one of: 'hr_admin', 'manager', 'employee', or 'unknown'

    Note: Uses user_has_role() which returns True when auth is disabled,
    giving full hr_admin access in that case.
    """
    user = get_current_user()
    employee_id = user.get("employeeId")

    # Determine persona using user_has_role (handles auth-disabled case)
    if user_has_role("hr_admin"):
        persona = "hr_admin"
    elif user_has_role("manager"):
        persona = "manager"
    elif user_has_role("employee"):
        persona = "employee"
    else:
        persona = "unknown"

    return employee_id, persona
