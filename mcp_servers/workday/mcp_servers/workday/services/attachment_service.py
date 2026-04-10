"""Service layer for attachment operations.

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

from typing import Any

from db.models import HelpCase
from db.repositories.help.attachment_repository import HelpAttachmentRepository
from db.repositories.help.audit_repository import HelpAuditRepository
from db.repositories.help.timeline_repository import HelpTimelineRepository
from db.session import get_session
from validators.business_rules import normalize_persona


class AttachmentService:
    """Service for attachment management with persona-based access control."""

    def _validate_persona_permission(self, actor_persona: str, action: str) -> None:
        """Validate persona has permission to add attachments."""
        allowed_personas = {"case_owner", "hr_admin"}
        if actor_persona not in allowed_personas:
            raise ValueError(
                f"E_AUTH_002: Insufficient permissions. Persona '{actor_persona}' "
                f"cannot perform '{action}'."
            )

    def add_attachment(
        self,
        case_id: str,
        filename: str,
        uploader: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        source: str | None = None,
        external_reference: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor_persona: str = "case_owner",
    ) -> dict[str, Any]:
        """
        Add an attachment to a case with persona validation.

        This is an atomic operation that:
        1. Validates persona permissions (only case_owner/hr_admin)
        2. Validates required fields (filename, uploader)
        3. Inserts the attachment
        4. Creates a timeline event (attachment_added)
        5. Creates an audit log entry

        Args:
            case_id: Case identifier
            filename: Attachment filename (required)
            uploader: Email/ID of person who uploaded the attachment (required)
            mime_type: Optional MIME type
            size_bytes: Optional file size in bytes
            source: Optional source (e.g., "ATS", "Background Check Vendor")
            external_reference: Optional external reference URL
            metadata: Optional metadata dictionary
            actor_persona: Persona of the actor adding the attachment

        Returns:
            Dictionary with attachment data

        Raises:
            ValueError: If validation fails, case doesn't exist, or persona lacks permission
        """
        # Validate persona permission
        self._validate_persona_permission(actor_persona, "add_attachment")

        # Validate required fields
        if not filename or not filename.strip():
            raise ValueError("E_ATT_002: Missing filename")

        if not uploader or not uploader.strip():
            raise ValueError("E_ATT_002: Missing uploader")

        attachment_repo = HelpAttachmentRepository()
        timeline_repo = HelpTimelineRepository()
        audit_repo = HelpAuditRepository()

        with get_session() as session:
            # Verify case exists
            case = session.get(HelpCase, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            # Add attachment
            attachment = attachment_repo.add_attachment(
                session=session,
                case_id=case_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=size_bytes,
                source=source,
                external_reference=external_reference,
                uploader=uploader,
                metadata=metadata,
            )

            # Add timeline event
            timeline_repo.add_event(
                session=session,
                case_id=case_id,
                event_type="attachment_added",
                actor=uploader,
                notes=f"Attachment added: {filename}",
                metadata={
                    "attachment_id": attachment["attachment_id"],
                    "filename": filename,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "source": source,
                },
            )

            # Add audit log entry
            audit_repo.insert_audit_log(
                session=session,
                case_id=case_id,
                entity_type="attachment",
                entity_id=attachment["attachment_id"],
                action="created",
                actor=uploader,
                actor_persona=normalize_persona(actor_persona),
                changes={
                    "filename": filename,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "source": source,
                },
                rationale=f"Added attachment: {filename}",
                metadata=metadata,
            )

            return attachment

    def list_attachments(
        self,
        case_id: str,
        cursor: str | None = None,
        limit: int = 50,
        created_before: str | None = None,
    ) -> dict[str, Any]:
        """
        List attachments for a case with pagination.

        Args:
            case_id: Case identifier
            cursor: Optional pagination cursor
            limit: Maximum number of attachments (default 50, max 200)
            created_before: Optional date filter

        Returns:
            Dictionary with attachments, pagination info
        """
        # Enforce max limit
        limit = min(limit, 200)

        attachment_repo = HelpAttachmentRepository()

        with get_session() as session:
            # Verify case exists
            case = session.get(HelpCase, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            attachments, next_cursor, has_more = attachment_repo.list_attachments(
                session=session,
                case_id=case_id,
                cursor=cursor,
                limit=limit,
                created_before=created_before,
            )

            return {
                "attachments": attachments,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
            }
