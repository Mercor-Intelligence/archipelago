"""Repository for help attachments using SQLAlchemy.

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

from db.models import HelpAttachment


def _utc_now_str() -> str:
    """Get current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


def _generate_attachment_id() -> str:
    """Generate a unique attachment ID."""
    return f"ATT-{uuid4().hex[:12].upper()}"


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


def _attachment_to_dict(attachment: HelpAttachment) -> dict[str, Any]:
    """Convert HelpAttachment model to dict."""
    return {
        "attachment_id": attachment.attachment_id,
        "case_id": attachment.case_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "source": attachment.source,
        "external_reference": attachment.external_reference,
        "uploader": attachment.uploader,
        "uploaded_at": attachment.uploaded_at,
        "metadata": _parse_metadata(attachment.meta),
    }


class HelpAttachmentRepository:
    """Repository for help attachment operations."""

    def add_attachment(
        self,
        session: Session,
        case_id: str,
        filename: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        source: str | None = None,
        external_reference: str | None = None,
        uploader: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add an attachment to a case."""
        uploaded_at = _utc_now_str()
        max_retries = 5

        for attempt in range(max_retries):
            attachment_id = _generate_attachment_id()
            attachment = HelpAttachment(
                attachment_id=attachment_id,
                case_id=case_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=size_bytes,
                source=source,
                external_reference=external_reference,
                uploader=uploader,
                uploaded_at=uploaded_at,
                meta=_serialize_metadata(metadata),
            )
            try:
                # Use savepoint to isolate this insert from the rest of the transaction.
                # If IntegrityError occurs, only this savepoint is rolled back,
                # preserving any earlier work in the same session.
                with session.begin_nested():
                    session.add(attachment)
                    session.flush()
                break
            except IntegrityError:
                # Savepoint was automatically rolled back, retry with new ID
                if attempt == max_retries - 1:
                    raise
                continue

        return {
            "attachment_id": attachment_id,
            "case_id": case_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "source": source,
            "external_reference": external_reference,
            "uploader": uploader,
            "uploaded_at": uploaded_at,
            "metadata": metadata,
        }

    def list_attachments(
        self,
        session: Session,
        case_id: str,
        cursor: str | None = None,
        limit: int = 50,
        created_before: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """List attachments for a case with pagination."""
        stmt = select(HelpAttachment).where(HelpAttachment.case_id == case_id)

        if created_before:
            stmt = stmt.where(HelpAttachment.uploaded_at <= created_before)

        if cursor:
            try:
                cursor_timestamp, cursor_attachment_id = cursor.split("|", 1)
                stmt = stmt.where(
                    (HelpAttachment.uploaded_at < cursor_timestamp)
                    | (
                        (HelpAttachment.uploaded_at == cursor_timestamp)
                        & (HelpAttachment.attachment_id < cursor_attachment_id)
                    )
                )
            except ValueError:
                stmt = stmt.where(HelpAttachment.uploaded_at < cursor)

        stmt = stmt.order_by(HelpAttachment.uploaded_at.desc(), HelpAttachment.attachment_id.desc())
        stmt = stmt.limit(limit + 1)

        rows = session.execute(stmt).scalars().all()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last_attachment = rows[-1]
            next_cursor = f"{last_attachment.uploaded_at}|{last_attachment.attachment_id}"

        attachments = [_attachment_to_dict(att) for att in rows]
        return attachments, next_cursor, has_more
