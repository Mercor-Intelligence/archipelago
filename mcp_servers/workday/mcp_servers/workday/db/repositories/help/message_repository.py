"""Repository for help messages using SQLAlchemy.

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

from db.models import HelpMessage


def _utc_now_str() -> str:
    """Get current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


def _generate_message_id() -> str:
    """Generate a unique message ID."""
    return f"MSG-{uuid4().hex[:12].upper()}"


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


def _message_to_dict(message: HelpMessage) -> dict[str, Any]:
    """Convert HelpMessage model to dict."""
    return {
        "message_id": message.message_id,
        "case_id": message.case_id,
        "direction": message.direction,
        "sender": message.sender,
        "audience": message.audience,
        "body": message.body,
        "created_at": message.created_at,
        "metadata": _parse_metadata(message.meta),
    }


class HelpMessageRepository:
    """Repository for help message operations."""

    def add_message(
        self,
        session: Session,
        case_id: str,
        direction: str,
        sender: str,
        body: str,
        audience: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a message to a case."""
        created_at = _utc_now_str()
        max_retries = 5

        for attempt in range(max_retries):
            message_id = _generate_message_id()
            message = HelpMessage(
                message_id=message_id,
                case_id=case_id,
                direction=direction,
                sender=sender,
                audience=audience,
                body=body,
                created_at=created_at,
                meta=_serialize_metadata(metadata),
            )
            try:
                # Use savepoint to isolate this insert from the rest of the transaction.
                # If IntegrityError occurs, only this savepoint is rolled back,
                # preserving any earlier work in the same session.
                with session.begin_nested():
                    session.add(message)
                    session.flush()
                break
            except IntegrityError:
                # Savepoint was automatically rolled back, retry with new ID
                if attempt == max_retries - 1:
                    raise
                continue

        return {
            "message_id": message_id,
            "case_id": case_id,
            "direction": direction,
            "sender": sender,
            "audience": audience,
            "body": body,
            "created_at": created_at,
            "metadata": metadata,
        }

    def get_message(self, session: Session, message_id: str) -> dict[str, Any] | None:
        """Get a message by ID."""
        message = session.get(HelpMessage, message_id)
        if message is None:
            return None
        return _message_to_dict(message)

    def search_messages(
        self,
        session: Session,
        message_id: str | None = None,
        case_id: str | None = None,
        direction: str | None = None,
        sender: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Search messages with filters and pagination."""
        stmt = select(HelpMessage)

        if message_id:
            stmt = stmt.where(HelpMessage.message_id == message_id)
        if case_id:
            stmt = stmt.where(HelpMessage.case_id == case_id)
        if direction:
            stmt = stmt.where(HelpMessage.direction == direction)
        if sender:
            stmt = stmt.where(HelpMessage.sender == sender)
        if created_after:
            stmt = stmt.where(HelpMessage.created_at >= created_after)
        if created_before:
            stmt = stmt.where(HelpMessage.created_at <= created_before)

        if cursor:
            try:
                cursor_timestamp, cursor_message_id = cursor.split("|", 1)
                stmt = stmt.where(
                    (HelpMessage.created_at < cursor_timestamp)
                    | (
                        (HelpMessage.created_at == cursor_timestamp)
                        & (HelpMessage.message_id < cursor_message_id)
                    )
                )
            except ValueError:
                stmt = stmt.where(HelpMessage.created_at < cursor)

        stmt = stmt.order_by(HelpMessage.created_at.desc(), HelpMessage.message_id.desc())
        stmt = stmt.limit(limit + 1)

        rows = session.execute(stmt).scalars().all()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last_message = rows[-1]
            next_cursor = f"{last_message.created_at}|{last_message.message_id}"

        messages = [_message_to_dict(msg) for msg in rows]
        return messages, next_cursor, has_more
