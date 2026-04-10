"""Repository for help timeline events using SQLAlchemy (append-only).

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import HelpAttachment, HelpCase, HelpMessage, HelpTimelineEvent


def _utc_now_str() -> str:
    """Get current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


def _generate_event_id() -> str:
    """Generate a unique event ID."""
    return f"EVT-{uuid4().hex[:12].upper()}"


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


def _event_to_dict(event: HelpTimelineEvent) -> dict[str, Any]:
    """Convert HelpTimelineEvent model to dict."""
    return {
        "event_id": event.event_id,
        "case_id": event.case_id,
        "event_type": event.event_type,
        "actor": event.actor,
        "created_at": event.created_at,
        "notes": event.notes,
        "metadata": _parse_metadata(event.meta),
    }


class HelpTimelineRepository:
    """Repository for help timeline events with append-only immutability."""

    def add_event(
        self,
        session: Session,
        case_id: str,
        event_type: str,
        actor: str,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add an immutable timeline event."""
        created_at = _utc_now_str()
        max_retries = 5

        for attempt in range(max_retries):
            event_id = _generate_event_id()
            event = HelpTimelineEvent(
                event_id=event_id,
                case_id=case_id,
                event_type=event_type,
                actor=actor,
                created_at=created_at,
                notes=notes,
                meta=_serialize_metadata(metadata),
            )
            try:
                # Use savepoint to isolate this insert from the rest of the transaction.
                # If IntegrityError occurs, only this savepoint is rolled back,
                # preserving any earlier work in the same session.
                with session.begin_nested():
                    session.add(event)
                    session.flush()
                break
            except IntegrityError:
                # Savepoint was automatically rolled back, retry with new ID
                if attempt == max_retries - 1:
                    raise
                continue

        return {
            "event_id": event_id,
            "case_id": case_id,
            "event_type": event_type,
            "actor": actor,
            "created_at": created_at,
            "notes": notes,
            "metadata": metadata,
        }

    def get_events(
        self,
        session: Session,
        case_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Get timeline events for a case with pagination."""
        stmt = select(HelpTimelineEvent).where(HelpTimelineEvent.case_id == case_id)

        if cursor:
            try:
                cursor_timestamp, cursor_event_id = cursor.split("|", 1)
                stmt = stmt.where(
                    (HelpTimelineEvent.created_at > cursor_timestamp)
                    | (
                        (HelpTimelineEvent.created_at == cursor_timestamp)
                        & (HelpTimelineEvent.event_id > cursor_event_id)
                    )
                )
            except ValueError:
                stmt = stmt.where(HelpTimelineEvent.created_at > cursor)

        stmt = stmt.order_by(HelpTimelineEvent.created_at.asc(), HelpTimelineEvent.event_id.asc())
        stmt = stmt.limit(limit + 1)

        rows = session.execute(stmt).scalars().all()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last_event = rows[-1]
            next_cursor = f"{last_event.created_at}|{last_event.event_id}"

        events = [_event_to_dict(event) for event in rows]
        return events, next_cursor, has_more

    def get_snapshot(
        self,
        session: Session,
        case_id: str,
        as_of_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get a full timeline snapshot for a case."""
        stmt = select(HelpTimelineEvent).where(HelpTimelineEvent.case_id == case_id)

        if as_of_date:
            stmt = stmt.where(HelpTimelineEvent.created_at <= as_of_date)

        stmt = stmt.order_by(HelpTimelineEvent.created_at.asc(), HelpTimelineEvent.event_id.asc())

        rows = session.execute(stmt).scalars().all()
        return [_event_to_dict(event) for event in rows]

    def get_event_count(self, session: Session, case_id: str) -> int:
        """Get total count of events for a case."""
        stmt = (
            select(func.count())
            .select_from(HelpTimelineEvent)
            .where(HelpTimelineEvent.case_id == case_id)
        )
        return session.execute(stmt).scalar() or 0

    def get_complete_snapshot(
        self,
        session: Session,
        case_id: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        """Get complete case snapshot in a single transaction."""
        # Fetch case
        case = session.get(HelpCase, case_id)
        if case is None:
            raise ValueError(f"E_CASE_001: Case not found: {case_id}")

        case_data = {
            "case_id": case.case_id,
            "case_type": case.case_type,
            "owner": case.owner,
            "status": case.status,
            "candidate_identifier": case.candidate_identifier,
            "due_date": case.due_date,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "metadata": _parse_metadata(case.meta),
        }

        # Fetch timeline events
        timeline_stmt = select(HelpTimelineEvent).where(HelpTimelineEvent.case_id == case_id)
        if as_of_date:
            timeline_stmt = timeline_stmt.where(HelpTimelineEvent.created_at <= as_of_date)
        timeline_stmt = timeline_stmt.order_by(
            HelpTimelineEvent.created_at.asc(), HelpTimelineEvent.event_id.asc()
        )
        timeline_events = [
            _event_to_dict(e) for e in session.execute(timeline_stmt).scalars().all()
        ]

        # Fetch messages
        message_stmt = select(HelpMessage).where(HelpMessage.case_id == case_id)
        if as_of_date:
            message_stmt = message_stmt.where(HelpMessage.created_at <= as_of_date)
        message_stmt = message_stmt.order_by(
            HelpMessage.created_at.desc(), HelpMessage.message_id.desc()
        )
        messages = [
            {
                "message_id": m.message_id,
                "case_id": m.case_id,
                "direction": m.direction,
                "sender": m.sender,
                "audience": m.audience,
                "body": m.body,
                "created_at": m.created_at,
                "metadata": _parse_metadata(m.meta),
            }
            for m in session.execute(message_stmt).scalars().all()
        ]

        # Fetch attachments
        attachment_stmt = select(HelpAttachment).where(HelpAttachment.case_id == case_id)
        if as_of_date:
            attachment_stmt = attachment_stmt.where(HelpAttachment.uploaded_at <= as_of_date)
        attachment_stmt = attachment_stmt.order_by(
            HelpAttachment.uploaded_at.desc(), HelpAttachment.attachment_id.desc()
        )
        attachments = [
            {
                "attachment_id": a.attachment_id,
                "case_id": a.case_id,
                "filename": a.filename,
                "mime_type": a.mime_type,
                "size_bytes": a.size_bytes,
                "source": a.source,
                "external_reference": a.external_reference,
                "uploader": a.uploader,
                "uploaded_at": a.uploaded_at,
                "metadata": _parse_metadata(a.meta),
            }
            for a in session.execute(attachment_stmt).scalars().all()
        ]

        return {
            "case": case_data,
            "timeline_events": timeline_events,
            "messages": messages,
            "attachments": attachments,
        }
