"""Audit history tools for the USPTO MCP server."""

from __future__ import annotations

import base64
import json
from typing import Annotated

from loguru import logger
from pydantic import Field

from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.db import get_db
from mcp_servers.uspto.models import (
    AuditEntry,
    AuditHistoryResponse,
    GetAuditHistoryRequest,
    PaginationResponse,
)
from mcp_servers.uspto.repositories.audit import AuditRepository
from mcp_servers.uspto.utils.errors import (
    NotFoundError,
    RateLimitError,
    ValidationError,
    handle_errors,
)


def _ensure_utc_timestamp(timestamp: str) -> str:
    """Convert timestamp to ISO 8601 format (T separator, Z suffix)."""
    # Replace space with T for SQLite datetime format
    result = timestamp.replace(" ", "T")
    # Replace +00:00 offset with Z (equivalent)
    result = result.replace("+00:00", "Z")
    # Only append Z if no timezone indicator present
    if not result.endswith("Z") and "+" not in result and "-" not in result[-6:]:
        result = f"{result}Z"
    return result


def _normalize_filter_date(date_str: str) -> str:
    """Normalize ISO 8601 filter date to SQLite format for comparison.

    Converts any timezone to UTC and formats as SQLite datetime (space separator, no TZ).
    """
    from datetime import UTC, datetime

    # Parse ISO 8601 timestamp
    # Replace Z with +00:00 for fromisoformat compatibility
    normalized = date_str.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(normalized)
        # Convert to UTC if timezone-aware
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        # Format as SQLite datetime (space separator, no timezone)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Fallback: simple string replacement if parsing fails
        result = date_str.replace("T", " ")
        if result.endswith("Z"):
            result = result[:-1]
        return result


@handle_errors
async def uspto_audit_workspace_history(
    workspace_id: Annotated[
        str,
        Field(
            pattern=r"^ws_[a-f0-9]{12}$",
            description="Workspace whose audit history is requested.",
        ),
    ],
    start_date: Annotated[
        str | None,
        Field(description="ISO 8601 start timestamp filter (YYYY-MM-DDTHH:MM:SS[.ffffff])."),
    ] = None,
    end_date: Annotated[
        str | None,
        Field(description="ISO 8601 end timestamp filter (YYYY-MM-DDTHH:MM:SS[.ffffff])."),
    ] = None,
    cursor: Annotated[
        str | None,
        Field(description="Cursor returned by a previous audit history page."),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=500, description="Maximum audit entries per page (1-500)."),
    ] = 100,
) -> AuditHistoryResponse:
    """Retrieve the append-only audit log for a workspace.

    Returns chronological entries of all workspace mutations and retrieval actions
    including: workspace_created, query_saved, query_executed, snapshot_created, etc.

    FILTERING: Use start_date and end_date (ISO 8601 format) to narrow results.
    Both date-only ('2024-01-15') and full timestamp formats are accepted.

    PAGINATION: Default limit is 100, max is 500. When has_more=true, use next_cursor
    to continue. Do NOT construct cursor values manually.

    USE CASE: Compliance tracking, research provenance, debugging workflow issues.

    COMMON ERRORS:
    - NOT_FOUND: workspace_id does not exist
    - INVALID_CURSOR: Malformed or expired pagination cursor
    - RATE_LIMIT_EXCEEDED: Too many requests (audit: 50/min)
    """
    request = GetAuditHistoryRequest(
        workspace_id=workspace_id,
        start_date=start_date,
        end_date=end_date,
        cursor=cursor,
        limit=limit,
    )
    # 1. Check session-scoped rate limit (audit: 50/min per BUILD_PLAN.md)
    rate_limit = rate_limiter.check_rate_limit("audit")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    # 3. Validate limit
    limit = min(request.limit, 500)

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
        repo = AuditRepository(session)

        # 5. Verify workspace exists
        if not await repo.workspace_exists(request.workspace_id):
            raise NotFoundError("workspace", request.workspace_id)

        # 6. Normalize filter dates to SQLite format for comparison
        start_date = _normalize_filter_date(request.start_date) if request.start_date else None
        end_date = _normalize_filter_date(request.end_date) if request.end_date else None

        # 7. Fetch audit entries from session database
        audit_logs = await repo.list_audit_entries(
            workspace_id=request.workspace_id,
            start_date=start_date,
            end_date=end_date,
            offset=offset,
            limit=limit + 1,  # Fetch one extra to check for more
        )

        # 8. Check if more results
        has_more = len(audit_logs) > limit
        if has_more:
            audit_logs = audit_logs[:limit]

        # 9. Generate next cursor
        next_cursor = None
        if has_more:
            next_offset = offset + limit
            next_cursor = base64.b64encode(str(next_offset).encode()).decode()

        # 10. Convert to response format
        audit_entries = []
        for log in audit_logs:
            details = json.loads(log.details) if log.details else {}
            audit_entries.append(
                AuditEntry(
                    audit_id=f"audit_{log.id}",
                    timestamp=_ensure_utc_timestamp(log.created_at),
                    action=log.action,
                    resource_type=log.resource_type or "",
                    resource_id=log.resource_id,
                    details=details,
                )
            )

        # 11. Log retrieval
        logger.info(
            f"Retrieved {len(audit_entries)} audit entries",
            workspace_id=request.workspace_id,
            count=len(audit_entries),
            has_more=has_more,
        )

        return AuditHistoryResponse(
            workspace_id=request.workspace_id,
            audit_entries=audit_entries,
            pagination=PaginationResponse(
                next_cursor=next_cursor,
                has_more=has_more,
            ),
        )


__all__ = ["uspto_audit_workspace_history"]
