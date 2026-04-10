"""Authentication middleware for Workday HCM MCP server.

Uses the shared mcp_auth package from packages/mcp_auth/
to enable persona-based authentication and RBAC.

Implements persona-based authorization for Help module with 4 personas:
- Case Owner: Full read/write access to cases
- HR Admin: Full read/write access to cases
- Manager: Read-only access + can log messages
- HR Analyst: Read-only access (no writes)
"""

from pathlib import Path
from typing import TYPE_CHECKING

from mcp_auth import AuthGuard, AuthService

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Path to users.json relative to this file
USERS_FILE = Path(__file__).parent.parent / "users.json"


def setup_auth(mcp_instance: "FastMCP") -> tuple["AuthService", "AuthGuard"]:
    """Configure authentication and authorization for the Workday HCM MCP server.

    Args:
        mcp_instance: FastMCP instance to configure with authentication.

    Returns:
        A tuple of (AuthService, AuthGuard) for further configuration if needed.
    """
    auth_service = AuthService(USERS_FILE)

    # Add AuthGuard middleware with auto-discovery
    auth_guard = AuthGuard(
        auth_service,
        mcp_instance=mcp_instance,  # Auto-discovers permissions from decorators
        public_tools=[
            "login_tool",
            "list_users",
            "workday_health_check",
            "get_server_info",
        ],  # Tools that don't require auth
        default_deny=True,  # Enforce permission matrix - deny tools without explicit permissions
    )

    # Help tools handle their own scope checks internally via _check_scope().
    # Add them with empty permissions so AuthGuard allows them through
    # (requires authentication but no specific scopes at middleware level).
    help_tools = [
        "workday_help_cases_create",
        "workday_help_cases_get",
        "workday_help_cases_update_status",
        "workday_help_cases_reassign_owner",
        "workday_help_cases_update_due_date",
        "workday_help_cases_search",
        "workday_help_timeline_add_event",
        "workday_help_timeline_get_events",
        "workday_help_timeline_get_snapshot",
        "workday_help_messages_add",
        "workday_help_messages_search",
        "workday_help_attachments_add",
        "workday_help_attachments_list",
        "workday_help_audit_query_history",
    ]
    for tool_name in help_tools:
        auth_guard.tool_permissions[tool_name] = {
            "scopes": set(),
            "any_scopes": set(),
            "roles": set(),
        }

    mcp_instance.add_middleware(auth_guard)

    return auth_service, auth_guard


__all__ = ["setup_auth", "USERS_FILE"]
