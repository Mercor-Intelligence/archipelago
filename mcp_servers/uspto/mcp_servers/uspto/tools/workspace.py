"""Workspace management tools for the USPTO MCP server."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Annotated

from loguru import logger
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.models import (
    CreateWorkspaceRequest,
    GetWorkspaceRequest,
    ListWorkspacesRequest,
    ListWorkspacesResponse,
    PaginationResponse,
    RecentActivityItem,
    WorkspaceResponse,
    WorkspaceStats,
)
from mcp_servers.uspto.repositories.workspace import WorkspaceRepository
from mcp_servers.uspto.utils.audit import log_audit_event
from mcp_servers.uspto.utils.errors import (
    NotFoundError,
    RateLimitError,
    ValidationError,
    WorkspaceConflictError,
    handle_errors,
)


def _ensure_utc_timestamp(timestamp: str) -> str:
    """Convert timestamp to ISO 8601 format (T separator, Z suffix)."""
    # SQLite datetime() uses space separator; ISO 8601 requires T
    result = timestamp.replace(" ", "T")
    if not result.endswith("Z"):
        result = f"{result}Z"
    return result


@handle_errors
async def uspto_workspaces_create(
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="Display name for the workspace (1-200 characters).",
        ),
    ],
    description: Annotated[
        str | None,
        Field(
            max_length=1000,
            description="Optional workspace description (max 1000 characters).",
        ),
    ] = None,
    metadata: Annotated[
        dict[str, str | int | bool] | None,
        Field(
            description="Optional key/value metadata scoped to the current session.",
        ),
    ] = None,
) -> WorkspaceResponse:
    """Create a new workspace for organizing patent research.

    Workspaces provide isolated containers for saved queries, snapshots, and audit history.
    Each workspace has a unique name and returns a workspace_id (format: 'ws_' + 12 hex chars)
    required for all subsequent operations.

    COMMON ERRORS:
    - WORKSPACE_CONFLICT: Name already exists in session (names must be unique)
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = CreateWorkspaceRequest(name=name, description=description, metadata=metadata)
    # 1. Check session-scoped rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        repo = WorkspaceRepository(session)

        # 3. Validate workspace name uniqueness within session
        existing = await repo.get_workspace_by_name(request.name)
        if existing:
            raise WorkspaceConflictError(workspace_name=request.name)

        # 4. Create workspace
        workspace_id = f"ws_{uuid.uuid4().hex[:12]}"
        try:
            workspace = await repo.create_workspace(
                id=workspace_id,
                name=request.name,
                description=request.description,
                metadata=json.dumps(request.metadata) if request.metadata else None,
            )
        except IntegrityError:
            # Race condition: another request created same name between check and insert
            raise WorkspaceConflictError(workspace_name=request.name)

        # 5. Log audit event (participates in current transaction)
        await log_audit_event(
            session,
            action="workspace_created",
            resource_type="workspace",
            resource_id=workspace_id,
            workspace_id=workspace_id,
            details={"name": request.name},
        )

        # 6. Return response
        logger.info(
            f"Created workspace: {workspace.name}",
            workspace_id=workspace.id,
            name=workspace.name,
        )

        return WorkspaceResponse(
            workspace_id=workspace.id,
            name=workspace.name,
            description=workspace.description,
            metadata=json.loads(workspace.metadata_json) if workspace.metadata_json else None,
            created_at=_ensure_utc_timestamp(workspace.created_at),
            updated_at=_ensure_utc_timestamp(workspace.updated_at),
            stats=WorkspaceStats(
                saved_queries=0,
                snapshots=0,
                documents_retrieved=0,
                foreign_priority_records=0,
            ),
        )


@handle_errors
async def uspto_workspaces_get(
    workspace_id: Annotated[
        str,
        Field(
            pattern=r"^ws_[a-f0-9]{12}$",
            description="Session workspace identifier (ws_{uuid}).",
        ),
    ],
) -> WorkspaceResponse:
    """Retrieve detailed information about a specific workspace.

    Returns workspace metadata, statistics (saved_queries, snapshots, documents_retrieved,
    foreign_priority_records), and recent activity log.

    PREREQUISITE: Workspace must exist - use workspace_id from uspto_workspaces_create
    or uspto_workspaces_list.

    COMMON ERRORS:
    - NOT_FOUND: workspace_id does not exist
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = GetWorkspaceRequest(workspace_id=workspace_id)
    # 1. Check session-scoped rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    async with get_db() as session:
        repo = WorkspaceRepository(session)

        # 3. Fetch workspace from session database
        workspace = await repo.get_workspace(request.workspace_id)
        if not workspace:
            raise NotFoundError("workspace", request.workspace_id)

        # 4. Get statistics
        stats = await repo.get_workspace_stats(workspace.id)

        # 5. Get recent activity
        recent_activity = await repo.get_recent_activity(workspace.id, limit=5)

        # 6. Return response
        logger.info(
            f"Retrieved workspace: {workspace.name}",
            workspace_id=workspace.id,
            name=workspace.name,
        )

        # Format recent activity for response
        formatted_activity = [
            RecentActivityItem(
                action=activity["action"],
                application_number=activity.get("applicationNumber"),
                timestamp=_ensure_utc_timestamp(activity["timestamp"]),
            )
            for activity in recent_activity
        ]

        return WorkspaceResponse(
            workspace_id=workspace.id,
            name=workspace.name,
            description=workspace.description,
            metadata=json.loads(workspace.metadata_json) if workspace.metadata_json else None,
            created_at=_ensure_utc_timestamp(workspace.created_at),
            updated_at=_ensure_utc_timestamp(workspace.updated_at),
            stats=WorkspaceStats(
                saved_queries=stats["saved_queries"],
                snapshots=stats["snapshots"],
                documents_retrieved=stats.get("documents_retrieved", 0),
                foreign_priority_records=stats.get("foreign_priority_records", 0),
            ),
            recent_activity=formatted_activity,
        )


@handle_errors
async def uspto_workspaces_list(
    cursor: Annotated[
        str | None,
        Field(
            description="Opaque pagination cursor previously returned by a list call.",
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Maximum number of workspaces to return (1-100).",
        ),
    ] = 20,
) -> ListWorkspacesResponse:
    """List all workspaces in the current session with pagination.

    Returns workspace summaries with statistics. Use cursor-based pagination for
    large result sets - pass the 'next_cursor' from response to get subsequent pages.

    PAGINATION: Default limit is 100. When has_more=true, use next_cursor to continue.
    Do NOT construct cursor values manually - always use the exact string returned.

    COMMON ERRORS:
    - INVALID_CURSOR: Malformed or expired pagination cursor
    - RATE_LIMIT_EXCEEDED: Too many requests (retrieval: 100/min)
    """
    request = ListWorkspacesRequest(cursor=cursor, limit=limit)
    # 1. Check session-scoped rate limit
    rate_limit = rate_limiter.check_rate_limit("retrieval")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    # 3. Validate limit
    limit = min(request.limit, 100)

    # 4. Decode cursor (if provided)
    offset = 0
    if request.cursor:
        try:
            offset = int(base64.b64decode(request.cursor).decode())
        except Exception:
            raise ValidationError(
                code="INVALID_CURSOR",
                message=(
                    "Invalid pagination cursor. Use the next_cursor from a previous response."
                ),
            )
        if offset < 0:
            raise ValidationError(
                code="INVALID_CURSOR",
                message=(
                    "Invalid pagination cursor: offset cannot be negative. "
                    "Use the next_cursor from a previous response."
                ),
            )

    async with get_db() as session:
        repo = WorkspaceRepository(session)

        # 5. Fetch workspaces from session database
        workspaces = await repo.list_workspaces(
            offset=offset,
            limit=limit + 1,  # Fetch one extra to check for more
        )

        # 6. Check if more results
        has_more = len(workspaces) > limit
        if has_more:
            workspaces = workspaces[:limit]

        # 7. Generate next cursor
        next_cursor = None
        if has_more:
            next_offset = offset + limit
            next_cursor = base64.b64encode(str(next_offset).encode()).decode()

        # 8. Get stats for all workspaces in batch (avoids N+1 query)
        workspace_ids = [ws.id for ws in workspaces]
        batch_stats = await repo.get_batch_workspace_stats(workspace_ids)

        workspace_responses = []
        for workspace in workspaces:
            stats = batch_stats.get(workspace.id, {})
            metadata = json.loads(workspace.metadata_json) if workspace.metadata_json else None
            workspace_responses.append(
                WorkspaceResponse(
                    workspace_id=workspace.id,
                    name=workspace.name,
                    description=workspace.description,
                    metadata=metadata,
                    created_at=_ensure_utc_timestamp(workspace.created_at),
                    updated_at=_ensure_utc_timestamp(workspace.updated_at),
                    stats=WorkspaceStats(
                        saved_queries=stats.get("saved_queries", 0),
                        snapshots=stats.get("snapshots", 0),
                        documents_retrieved=stats.get("documents_retrieved", 0),
                        foreign_priority_records=stats.get("foreign_priority_records", 0),
                    ),
                )
            )

        # 9. Return response
        logger.info(
            f"Listed {len(workspace_responses)} workspaces",
            count=len(workspace_responses),
            has_more=has_more,
        )

        return ListWorkspacesResponse(
            workspaces=workspace_responses,
            pagination=PaginationResponse(
                next_cursor=next_cursor,
                has_more=has_more,
            ),
        )


__all__ = [
    "uspto_workspaces_create",
    "uspto_workspaces_get",
    "uspto_workspaces_list",
]
