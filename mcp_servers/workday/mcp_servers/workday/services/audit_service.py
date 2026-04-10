"""Service layer for audit log operations.

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from db.repositories.help.audit_repository import HelpAuditRepository
from db.session import get_session
from loguru import logger
from validators.business_rules import validate_date_range


class AuditService:
    """Service for audit log queries."""

    def query_history(
        self,
        case_id: str | None = None,
        actor: str | None = None,
        action_type: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Query audit log with filters and pagination.

        Args:
            case_id: Optional case identifier filter
            actor: Optional actor filter
            action_type: Optional action type filter
            created_after: Optional start date filter (ISO 8601)
            created_before: Optional end date filter (ISO 8601)
            cursor: Optional pagination cursor
            limit: Maximum number of entries (default 100, max 500)

        Returns:
            Dictionary with audit entries, pagination info

        Raises:
            ValueError: If date validation fails
        """
        if created_after:
            try:
                datetime.fromisoformat(created_after.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("E_VAL_001: created_after must be ISO 8601 format") from exc

        if created_before:
            try:
                datetime.fromisoformat(created_before.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("E_VAL_001: created_before must be ISO 8601 format") from exc

        if created_after and created_before:
            validate_date_range(created_after, created_before, "Date range")

        limit = min(limit, 500)

        audit_repo = HelpAuditRepository()

        with get_session() as session:
            logger.info(
                f"Running audit query_history: case_id={case_id}, actor={actor}, "
                f"action={action_type}, cursor={cursor}, limit={limit}"
            )
            entries, next_cursor, has_more = audit_repo.query_history(
                session=session,
                case_id=case_id,
                actor=actor,
                action_type=action_type,
                created_after=created_after,
                created_before=created_before,
                cursor=cursor,
                limit=limit,
            )
            logger.info(
                f"Audit query_history completed: results={len(entries)}, "
                f"next_cursor={next_cursor}, has_more={has_more}"
            )

            return {
                "audit_log": entries,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
            }
