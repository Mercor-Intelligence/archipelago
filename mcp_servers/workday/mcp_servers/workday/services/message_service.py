"""Service layer for message operations.

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

from typing import Any

from db.models import HelpCase
from db.repositories.help.audit_repository import HelpAuditRepository
from db.repositories.help.message_repository import HelpMessageRepository
from db.repositories.help.timeline_repository import HelpTimelineRepository
from db.session import get_session
from validators.business_rules import VALID_DIRECTIONS, ensure_enum, normalize_persona


class MessageService:
    """Service for message management with persona-based access control."""

    def _validate_external_message_permission(self, direction: str, persona: str) -> None:
        """
        Validate that persona has permission to add external messages.

        Args:
            direction: Message direction (internal/inbound/outbound)
            persona: Actor persona

        Raises:
            ValueError: If HR Analyst (read-only persona) tries to add external
                (inbound/outbound) messages
        """
        # HR Analysts are read-only for message creation (additional guard)
        restricted_personas = ("hr_analyst",)
        external_directions = ("inbound", "outbound")

        if persona in restricted_personas and direction in external_directions:
            raise ValueError(
                f"E_AUTH_002: Insufficient permissions. Persona '{persona}' "
                f"cannot add {direction} messages. Only internal messages are allowed."
            )

    def add_message(
        self,
        case_id: str,
        direction: str,
        sender: str,
        body: str,
        actor: str,
        audience: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor_persona: str = "case_owner",
    ) -> dict[str, Any]:
        """
        Add a message to a case with persona validation.

        This is an atomic operation that:
        1. Validates persona permissions
        2. Inserts the message
        3. Creates a timeline event
        4. Creates an audit log entry

        Args:
            case_id: Case identifier
            direction: Message direction (internal/inbound/outbound)
            sender: Message sender/author (may be external for inbound)
            body: Message body/content
            actor: System user performing this action (for audit trail)
            audience: Optional audience (required for inbound/outbound)
            metadata: Optional metadata dictionary
            actor_persona: Persona of the actor adding the message

        Returns:
            Dictionary with message data

        Raises:
            ValueError: If validation fails, case doesn't exist, or persona lacks permission
        """
        # Validate direction
        ensure_enum(direction, VALID_DIRECTIONS, "E_MSG_002")

        # Validate persona permission for external messages
        self._validate_external_message_permission(direction, actor_persona)

        message_repo = HelpMessageRepository()
        timeline_repo = HelpTimelineRepository()
        audit_repo = HelpAuditRepository()

        with get_session() as session:
            # Verify case exists
            case = session.get(HelpCase, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            # Add message
            message = message_repo.add_message(
                session=session,
                case_id=case_id,
                direction=direction,
                sender=sender,
                body=body,
                audience=audience,
                metadata=metadata,
            )

            # Add timeline event
            timeline_repo.add_event(
                session=session,
                case_id=case_id,
                event_type="message_added",
                actor=actor,
                notes=f"Message added: {direction}",
                metadata={
                    "message_id": message["message_id"],
                    "direction": direction,
                    "sender": sender,
                    "audience": audience,
                },
            )

            # Add audit log entry
            audit_repo.insert_audit_log(
                session=session,
                case_id=case_id,
                entity_type="message",
                entity_id=message["message_id"],
                action="created",
                actor=actor,
                actor_persona=normalize_persona(actor_persona),
                changes={"direction": direction, "sender": sender, "body_length": len(body)},
                rationale=f"Added {direction} message",
                metadata=metadata,
            )

            return message

    def get_message(self, message_id: str) -> dict[str, Any]:
        """
        Get a message by ID.

        Args:
            message_id: Message identifier

        Returns:
            Dictionary with message data

        Raises:
            ValueError: If message doesn't exist
        """
        message_repo = HelpMessageRepository()

        with get_session() as session:
            message = message_repo.get_message(session, message_id)

            if message is None:
                raise ValueError(f"E_MSG_001: Message not found: {message_id}")

            return message

    def search_messages(
        self,
        *,
        message_id: str | None = None,
        case_id: str | None = None,
        direction: str | None = None,
        sender: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Search messages with filters and pagination.

        Args:
            message_id: Optional message ID filter (exact match, raises E_MSG_001 if not found)
            case_id: Optional case ID filter
            direction: Optional direction filter
            sender: Optional sender filter
            created_after: Optional start date filter
            created_before: Optional end date filter
            cursor: Optional pagination cursor
            limit: Maximum number of messages (default 50, max 200)

        Returns:
            Dictionary with messages, pagination info
        """
        # Validate direction if provided
        if direction:
            ensure_enum(direction, VALID_DIRECTIONS, "E_MSG_002")

        limit = min(limit, 200)

        message_repo = HelpMessageRepository()

        with get_session() as session:
            messages, next_cursor, has_more = message_repo.search_messages(
                session=session,
                message_id=message_id,
                case_id=case_id,
                direction=direction,
                sender=sender,
                created_after=created_after,
                created_before=created_before,
                cursor=cursor,
                limit=limit,
            )

            if message_id and not messages:
                raise ValueError(f"E_MSG_001: Message not found: {message_id}")

            return {
                "messages": messages,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
            }
