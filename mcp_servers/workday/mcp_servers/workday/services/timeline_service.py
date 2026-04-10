"""Service layer for timeline operations.

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

import time
from typing import Any

from db.models import HelpCase
from db.repositories.help.timeline_repository import HelpTimelineRepository
from db.session import get_session
from loguru import logger
from validators.business_rules import VALID_EVENT_TYPES, ensure_enum


class TimelineService:
    """Service for timeline event management."""

    def add_event(
        self,
        case_id: str,
        event_type: str,
        actor: str,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Add a timeline event (append-only).

        Args:
            case_id: Case identifier
            event_type: Type of event (must be in VALID_EVENT_TYPES)
            actor: Actor who triggered the event
            notes: Optional notes
            metadata: Optional metadata dictionary

        Returns:
            Dictionary with event data

        Raises:
            ValueError: If validation fails or case doesn't exist
        """
        ensure_enum(event_type, VALID_EVENT_TYPES, "E_TML_002")

        timeline_repo = HelpTimelineRepository()

        with get_session() as session:
            case = session.get(HelpCase, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            event = timeline_repo.add_event(
                session=session,
                case_id=case_id,
                event_type=event_type,
                actor=actor,
                notes=notes,
                metadata=metadata,
            )
            return event

    def get_events(
        self,
        case_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Get timeline events for a case with pagination.

        Args:
            case_id: Case identifier
            cursor: Optional pagination cursor
            limit: Maximum number of events (default 100, max 500)

        Returns:
            Dictionary with events, pagination info

        Raises:
            ValueError: If case doesn't exist
        """
        limit = min(limit, 500)

        timeline_repo = HelpTimelineRepository()

        with get_session() as session:
            case = session.get(HelpCase, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            logger.info(
                "Executing timeline get_events query: "
                f"case_id={case_id}, cursor={cursor}, limit={limit}"
            )
            events, next_cursor, has_more = timeline_repo.get_events(
                session=session,
                case_id=case_id,
                cursor=cursor,
                limit=limit,
            )
            logger.info(
                f"Timeline get_events completed: case_id={case_id}, rows={len(events)}, "
                f"next_cursor={next_cursor}, has_more={has_more}"
            )

            return {
                "events": events,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
            }

    def get_snapshot(
        self,
        case_id: str,
        as_of_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieve timeline snapshot for a case up to the optional cutoff date.

        Args:
            case_id: Case identifier
            as_of_date: Optional ISO 8601 cutoff (inclusive)

        Returns:
            List of timeline events ordered chronologically
        """
        timeline_repo = HelpTimelineRepository()

        with get_session() as session:
            case = session.get(HelpCase, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            logger.info(
                f"Running timeline snapshot query: case_id={case_id}, as_of_date={as_of_date}"
            )
            events = timeline_repo.get_snapshot(
                session=session, case_id=case_id, as_of_date=as_of_date
            )
            logger.info(
                f"Timeline snapshot query completed: case_id={case_id}, events={len(events)}"
            )
            return events

    def get_complete_snapshot(
        self,
        case_id: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        """
        Retrieve complete case snapshot in a single transaction.

        Fetches case, timeline, messages, and attachments in one go for
        sub-2-second performance even with large datasets (100+ events,
        50+ messages/attachments).

        Args:
            case_id: Case identifier
            as_of_date: Optional ISO 8601 cutoff (inclusive) for filtering
                timeline/messages/attachments

        Returns:
            Dictionary with keys: 'case', 'timeline_events', 'messages', 'attachments'

        Raises:
            ValueError: If case doesn't exist
        """
        start_time = time.perf_counter()
        logger.info(f"Starting snapshot retrieval: case_id={case_id}, as_of_date={as_of_date}")

        try:
            timeline_repo = HelpTimelineRepository()

            with get_session() as session:
                snapshot = timeline_repo.get_complete_snapshot(
                    session=session, case_id=case_id, as_of_date=as_of_date
                )

                elapsed_ms = (time.perf_counter() - start_time) * 1000
                event_count = len(snapshot["timeline_events"])
                message_count = len(snapshot["messages"])
                attachment_count = len(snapshot["attachments"])

                logger.info(
                    f"Snapshot retrieved: case_id={case_id}, "
                    f"duration_ms={elapsed_ms:.2f}, "
                    f"events={event_count}, messages={message_count}, "
                    f"attachments={attachment_count}"
                )

                # Log performance checkpoint if approaching threshold
                if elapsed_ms > 1500:
                    logger.warning(
                        f"Snapshot retrieval approaching 2s threshold: "
                        f"case_id={case_id}, duration_ms={elapsed_ms:.2f}"
                    )

                return snapshot
        except ValueError:
            raise
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                f"Snapshot retrieval failed: case_id={case_id}, "
                f"duration_ms={elapsed_ms:.2f}, error={e}"
            )
            raise
