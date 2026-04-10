"""Timeline MCP tools for Workday Help."""

from loguru import logger
from mcp_auth import get_current_user
from schemas.help.case_schemas import GetCaseResponse
from schemas.help.timeline_schemas import (
    AddTimelineEventRequest,
    AddTimelineEventResponse,
    AttachmentSnapshotEntry,
    GetTimelineEventsRequest,
    GetTimelineEventsResponse,
    GetTimelineSnapshotRequest,
    GetTimelineSnapshotResponse,
    MessageSnapshotEntry,
    TimelineSnapshotEvent,
)
from services.attachment_service import AttachmentService
from services.case_service import CaseService
from services.message_service import MessageService
from services.timeline_service import TimelineService
from utils.decorators import make_async_background
from validators.business_rules import SUPPORTED_PERSONAS

_timeline_service = TimelineService()
_message_service = MessageService()
_attachment_service = AttachmentService()
_case_service = CaseService()

# Personas allowed to add timeline events directly
_TIMELINE_WRITE_PERSONAS = {"case_owner", "hr_admin"}


def _derive_context(
    actor: str | None,
    *,
    default_persona: str = "case_owner",
) -> tuple[str, str]:
    """Resolve persona/actor using request values with auth context fallback.

    If no actor is provided, uses the authenticated user's identity.
    Searches user's roles for a compatible persona from SUPPORTED_PERSONAS.
    """
    user = get_current_user()

    # Resolve actor
    resolved_actor = actor or user.get("username") or user.get("userId")
    if not resolved_actor:
        raise ValueError("E_AUTH_002: actor is required for timeline operations")

    # Find a compatible persona from user's roles
    persona = None
    user_roles = user.get("roles") or []
    for role in user_roles:
        if role in SUPPORTED_PERSONAS:
            persona = role
            break
    if not persona:
        persona = default_persona

    return persona, resolved_actor


def _validate_timeline_write_permission(persona: str) -> None:
    """Validate that persona has permission to add timeline events directly.

    Only case_owner and hr_admin can add timeline events directly.
    Other personas (manager, hr_analyst) are read-only for timeline.
    """
    if persona not in _TIMELINE_WRITE_PERSONAS:
        raise ValueError(
            f"E_AUTH_002: Insufficient permissions. Persona '{persona}' "
            f"cannot add timeline events. Allowed: {_TIMELINE_WRITE_PERSONAS}"
        )


@make_async_background
def workday_help_timeline_add_event(request: AddTimelineEventRequest) -> AddTimelineEventResponse:
    """Add an immutable timeline event to a case."""
    # Derive persona and validate write permission
    persona, resolved_actor = _derive_context(request.actor)
    _validate_timeline_write_permission(persona)

    logger.info(
        f"Adding timeline event: case_id={request.case_id}, event_type={request.event_type}, "
        f"persona={persona}"
    )

    try:
        event = _timeline_service.add_event(
            case_id=request.case_id,
            event_type=request.event_type,
            actor=resolved_actor,
            notes=request.notes,
            metadata=request.metadata,
        )

        return AddTimelineEventResponse(
            event_id=event["event_id"],
            case_id=event["case_id"],
            event_type=event["event_type"],
            actor=event["actor"],
            created_at=event["created_at"],
            notes=event["notes"],
            metadata=event["metadata"],
        )
    except ValueError as e:
        logger.error(f"Error adding timeline event: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error adding timeline event: {e}")
        raise ValueError(f"E_GEN_001: Failed to add timeline event: {e}") from e


@make_async_background
def workday_help_timeline_get_events(
    request: GetTimelineEventsRequest,
) -> GetTimelineEventsResponse:
    """Get timeline events for a case with pagination."""
    logger.info(f"Getting timeline events: case_id={request.case_id}")

    try:
        result = _timeline_service.get_events(
            case_id=request.case_id,
            cursor=request.cursor,
            limit=request.limit,
        )

        events = [
            AddTimelineEventResponse(
                event_id=e["event_id"],
                case_id=e["case_id"],
                event_type=e["event_type"],
                actor=e["actor"],
                created_at=e["created_at"],
                notes=e["notes"],
                metadata=e["metadata"],
            )
            for e in result["events"]
        ]

        return GetTimelineEventsResponse(
            events=events,
            next_cursor=result["next_cursor"],
            has_more=result["has_more"],
            limit=result["limit"],
        )
    except ValueError as e:
        logger.error(f"Error getting timeline events: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting timeline events: {e}")
        raise ValueError(f"E_GEN_001: Failed to get timeline events: {e}") from e


@make_async_background
def workday_help_timeline_get_snapshot(
    request: GetTimelineSnapshotRequest,
) -> GetTimelineSnapshotResponse:
    """Return a case snapshot including all timeline data."""
    logger.info(
        f"Getting timeline snapshot: case_id={request.case_id}, as_of_date={request.as_of_date}"
    )

    try:
        # Use optimized single-transaction snapshot retrieval
        snapshot = _timeline_service.get_complete_snapshot(
            case_id=request.case_id,
            as_of_date=request.as_of_date,
        )

        # Transform timeline events
        timeline_entries = [
            TimelineSnapshotEvent(
                event_id=e["event_id"],
                event_type=e["event_type"],
                timestamp=e["created_at"],
                actor=e.get("actor"),
                notes=e.get("notes"),
            )
            for e in snapshot["timeline_events"]
        ]

        # Transform messages
        message_entries = [
            MessageSnapshotEntry(
                message_id=m["message_id"],
                direction=m["direction"],
                audience=m.get("audience"),
                content=m["body"],
                timestamp=m["created_at"],
            )
            for m in snapshot["messages"]
        ]

        # Transform attachments
        attachment_entries = [
            AttachmentSnapshotEntry(
                attachment_id=a["attachment_id"],
                filename=a["filename"],
                type=a.get("mime_type"),
                source=a.get("source"),
                uploaded_by=a.get("uploader"),
                timestamp=a["uploaded_at"],
            )
            for a in snapshot["attachments"]
        ]

        return GetTimelineSnapshotResponse(
            case=GetCaseResponse(**snapshot["case"]),
            as_of_date=request.as_of_date,
            timeline=timeline_entries,
            messages=message_entries,
            attachments=attachment_entries,
        )
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting timeline snapshot: {e}")
        raise ValueError(f"E_GEN_001: Failed to get timeline snapshot: {e}") from e
