"""Repository for help audit log using SQLAlchemy (append-only).

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import HelpAuditLog


def _utc_now_str() -> str:
    """Get current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


def _generate_log_id() -> str:
    """Generate a unique audit log ID."""
    return f"AUDIT-{uuid4().hex[:12].upper()}"


def _parse_metadata(meta_json: str | None) -> dict[str, Any] | None:
    """Parse JSON metadata string to dict."""
    if meta_json in (None, ""):
        return None
    return json.loads(meta_json)


def _serialize_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Serialize metadata dict to JSON string."""
    if metadata is None:
        return None
    return json.dumps(metadata, separators=(",", ":"), sort_keys=True)


def _audit_to_dict(entry: HelpAuditLog) -> dict[str, Any]:
    """Convert HelpAuditLog model to dict."""
    return {
        "log_id": entry.log_id,
        "case_id": entry.case_id,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "action": entry.action,
        "actor": entry.actor,
        "actor_persona": entry.actor_persona,
        "created_at": entry.created_at,
        "changes": _parse_metadata(entry.changes),
        "rationale": entry.rationale,
        "metadata": _parse_metadata(entry.meta),
    }


class HelpAuditRepository:
    """Repository for help audit log queries with append-only immutability."""

    def insert_audit_log(
        self,
        session: Session,
        case_id: str,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        actor_persona: str,
        changes: dict[str, Any] | None = None,
        rationale: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a new audit log entry."""
        created_at = _utc_now_str()
        max_retries = 5

        for attempt in range(max_retries):
            log_id = _generate_log_id()
            entry = HelpAuditLog(
                log_id=log_id,
                case_id=case_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor=actor,
                actor_persona=actor_persona,
                created_at=created_at,
                changes=_serialize_metadata(changes),
                rationale=rationale,
                meta=_serialize_metadata(metadata),
            )
            try:
                # Use savepoint to isolate this insert from the rest of the transaction.
                # If IntegrityError occurs, only this savepoint is rolled back,
                # preserving any earlier work in the same session.
                with session.begin_nested():
                    session.add(entry)
                    session.flush()
                break
            except IntegrityError:
                # Savepoint was automatically rolled back, retry with new ID
                if attempt == max_retries - 1:
                    raise
                continue

        return _audit_to_dict(entry)

    def query_history(
        self,
        session: Session,
        case_id: str | None = None,
        actor: str | None = None,
        action_type: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Query audit log with filters and pagination."""
        limit = min(limit, 500)

        stmt = select(HelpAuditLog)

        if case_id:
            stmt = stmt.where(HelpAuditLog.case_id == case_id)
        if actor:
            stmt = stmt.where(HelpAuditLog.actor == actor)
        if action_type:
            stmt = stmt.where(HelpAuditLog.action == action_type)
        if created_after:
            stmt = stmt.where(HelpAuditLog.created_at >= created_after)
        if created_before:
            stmt = stmt.where(HelpAuditLog.created_at <= created_before)

        if cursor:
            try:
                cursor_timestamp, cursor_log_id = cursor.split("|", 1)
                stmt = stmt.where(
                    (HelpAuditLog.created_at > cursor_timestamp)
                    | (
                        (HelpAuditLog.created_at == cursor_timestamp)
                        & (HelpAuditLog.log_id > cursor_log_id)
                    )
                )
            except ValueError:
                stmt = stmt.where(HelpAuditLog.created_at > cursor)

        stmt = stmt.order_by(HelpAuditLog.created_at.asc(), HelpAuditLog.log_id.asc())
        stmt = stmt.limit(limit + 1)

        rows = session.execute(stmt).scalars().all()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last_entry = rows[-1]
            next_cursor = f"{last_entry.created_at}|{last_entry.log_id}"

        entries = [_audit_to_dict(entry) for entry in rows]
        return entries, next_cursor, has_more
