"""Session-scoped audit logging for the USPTO MCP Server."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import AuditLog
from mcp_servers.uspto.utils.logging import redact_sensitive_data


async def log_audit_event(
    session: AsyncSession,
    action: str,
    resource_type: str,
    resource_id: str,
    workspace_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Log an audit event to the session-scoped database and logger.

    Session-scoped only - no user_id tracking.
    Audit log is cleared when session ends.

    Note:
        Does not commit the transaction. Caller is responsible for committing.

    Args:
        session: Database session (required - participates in caller's transaction)
        action: The action performed (e.g., "CREATE", "UPDATE", "DELETE")
        resource_type: Type of resource affected (e.g., "workspace", "query", "snapshot")
        resource_id: Identifier of the affected resource
        workspace_id: Optional workspace ID if action is scoped to a workspace
        details: Optional additional details about the action
    """
    # Redact sensitive data before storing in database and logging
    redacted_details = redact_sensitive_data(details) if details else None

    audit_entry = AuditLog(
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=json.dumps(redacted_details) if redacted_details else None,
    )
    session.add(audit_entry)

    logger.info(
        f"Audit: {action}",
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=redacted_details,
    )


__all__ = ["log_audit_event"]
