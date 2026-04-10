"""Permission constants for Greenhouse MCP Server tools.

These constants are used with @require_scopes decorators to declare
which scopes are needed for each tool.

Example:
    from mcp_auth import require_scopes
    from auth.permissions import Permission as Perm

    @require_scopes(Perm.CANDIDATE_READ.value)
    async def greenhouse_candidates_get(params: GetCandidateInput):
        ...
"""

from enum import Enum


class Permission(str, Enum):
    """Permission scopes for tool operations.

    These values must match the scopes defined in users.json.
    """

    # Candidate permissions
    CANDIDATE_READ = "candidate:read"
    CANDIDATE_CREATE = "candidate:create"
    CANDIDATE_UPDATE = "candidate:update"
    CANDIDATE_DELETE = "candidate:delete"
    CANDIDATE_ADD_NOTE = "candidate:add_note"
    CANDIDATE_ADD_TAG = "candidate:add_tag"

    # Application permissions
    APPLICATION_READ = "application:read"
    APPLICATION_CREATE = "application:create"
    APPLICATION_ADVANCE = "application:advance"
    APPLICATION_MOVE = "application:move"
    APPLICATION_REJECT = "application:reject"
    APPLICATION_HIRE = "application:hire"

    # Job permissions
    JOB_READ = "job:read"
    JOB_CREATE = "job:create"
    JOB_UPDATE = "job:update"

    # Feedback permissions
    FEEDBACK_READ = "feedback:read"
    FEEDBACK_SUBMIT = "feedback:submit"

    # User permissions
    USER_READ = "user:read"
    USER_CREATE = "user:create"

    # Activity permissions
    ACTIVITY_READ = "activity:read"
    AUDIT_READ = "audit:read"

    # Admin permissions
    RESET_STATE = "reset:state"
    EXPORT_SNAPSHOT = "admin:export"

    # Job Board permissions (public)
    JOBBOARD_READ = "jobboard:read"
    JOBBOARD_APPLY = "jobboard:apply"

    # Server info permissions (public)
    SERVER_INFO = "server:info"


__all__ = ["Permission"]
