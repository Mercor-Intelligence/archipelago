"""Service layer for Workday Help case management tools.

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from db.repositories.help.audit_repository import HelpAuditRepository
from db.repositories.help.case_repository import HelpCaseRepository
from db.repositories.help.timeline_repository import HelpTimelineRepository
from db.session import get_session
from sqlalchemy.exc import IntegrityError
from utils.help_status_machine import is_valid_status, validate_transition
from validators.business_rules import VALID_CASE_TYPES, VALID_STATUSES, normalize_persona


class CaseService:
    """Service for orchestrating case lifecycle operations."""

    @staticmethod
    def _assert_persona_allowed(persona: str, allowed: Iterable[str], action: str) -> None:
        if persona not in allowed:
            raise ValueError(f"E_AUTH_002: Persona '{persona}' cannot perform '{action}'")

    @staticmethod
    def _assert_case_owner_scope(persona: str, actor: str | None, owner: str, action: str) -> None:
        if persona != "case_owner":
            return
        if not actor:
            raise ValueError("E_AUTH_002: case_owner must specify an actor email to enforce scope")
        if actor != owner:
            raise ValueError(
                "E_AUTH_002: case_owner "
                f"'{actor}' cannot perform '{action}' "
                f"on cases owned by '{owner}'"
            )

    def _normalize_requested_case_id(self, case_id: str | None) -> str | None:
        """Normalize a requested case_id and ensure it is not empty if provided."""
        if case_id is None:
            return None
        normalized = case_id.strip()
        if not normalized:
            raise ValueError("E_VAL_001: case_id cannot be empty when provided")
        return normalized

    def create_case(
        self,
        *,
        case_type: str,
        owner: str,
        status: str,
        candidate_identifier: str,
        due_date: str | None = None,
        metadata: dict[str, Any] | None = None,
        requested_case_id: str | None = None,
        actor: str,
        actor_persona: str,
    ) -> dict[str, Any]:
        """Create a new case with timeline and audit entries."""
        self._assert_persona_allowed(
            actor_persona,
            {"case_owner", "hr_admin"},
            "create_case",
        )

        if case_type not in VALID_CASE_TYPES:
            raise ValueError("E_CASE_005: case_type must be 'Pre-Onboarding'")

        if status not in VALID_STATUSES:
            raise ValueError(
                f"E_CASE_002: Invalid case status. Must be one of: {list(VALID_STATUSES)}"
            )

        case_repo = HelpCaseRepository()
        timeline_repo = HelpTimelineRepository()
        audit_repo = HelpAuditRepository()

        with get_session() as session:
            if case_repo.exists_candidate(session, candidate_identifier):
                raise ValueError("E_CASE_004: Candidate identifier already in use")

            normalized_case_id = self._normalize_requested_case_id(requested_case_id)
            try:
                case = case_repo.create_case(
                    session=session,
                    case_id=normalized_case_id,
                    case_type=case_type,
                    owner=owner,
                    status=status,
                    candidate_identifier=candidate_identifier,
                    due_date=due_date,
                    metadata=metadata,
                )
            except IntegrityError as exc:
                session.rollback()
                message = str(exc).lower()
                if "case_id" in message:
                    raise ValueError("E_CASE_004: Duplicate case ID") from exc
                if "candidate_identifier" in message:
                    raise ValueError("E_CASE_004: Candidate identifier already in use") from exc
                raise

            timeline_event = timeline_repo.add_event(
                session=session,
                case_id=case["case_id"],
                event_type="case_created",
                actor=actor,
                notes="Case created",
                metadata={
                    "candidate_identifier": candidate_identifier,
                    "case_type": case_type,
                    "status": status,
                },
            )

            audit_entry = audit_repo.insert_audit_log(
                session=session,
                case_id=case["case_id"],
                entity_type="case",
                entity_id=case["case_id"],
                action="case_created",
                actor=actor,
                actor_persona=normalize_persona(actor_persona),
                changes={"status": {"old": None, "new": status}},
                rationale="Case created",
                metadata=metadata,
            )

            return {
                "case": case,
                "timeline_event_id": timeline_event["event_id"],
                "audit_log_id": audit_entry["log_id"],
            }

    def get_case(
        self,
        *,
        case_id: str,
        actor_persona: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a case, enforcing persona scope."""
        self._assert_persona_allowed(
            actor_persona,
            {"case_owner", "hr_admin", "manager", "hr_analyst"},
            "get_case",
        )

        case_repo = HelpCaseRepository()

        with get_session() as session:
            case = case_repo.get_case(session, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            self._assert_case_owner_scope(actor_persona, actor, case["owner"], "get_case")

            return case

    def update_status(
        self,
        *,
        case_id: str,
        current_status: str,
        new_status: str,
        rationale: str,
        actor: str,
        actor_persona: str,
    ) -> dict[str, Any]:
        """Transition case status with validation, timeline, and audit logging."""
        self._assert_persona_allowed(
            actor_persona,
            {"case_owner", "hr_admin"},
            "update_case_status",
        )

        if new_status not in VALID_STATUSES:
            raise ValueError(
                f"E_CASE_002: Invalid case status. Must be one of: {list(VALID_STATUSES)}"
            )

        validate_transition(current_status, new_status)

        case_repo = HelpCaseRepository()
        timeline_repo = HelpTimelineRepository()
        audit_repo = HelpAuditRepository()

        with get_session() as session:
            case = case_repo.get_case(session, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")
            self._assert_case_owner_scope(actor_persona, actor, case["owner"], "update_case_status")

            previous_status = case["status"]

            rows_updated = case_repo.update_status(
                session=session,
                case_id=case_id,
                current_status=current_status,
                new_status=new_status,
            )
            if rows_updated == 0:
                raise ValueError("E_CASE_006: Concurrent update conflict")

            timeline_event = timeline_repo.add_event(
                session=session,
                case_id=case_id,
                event_type="status_changed",
                actor=actor,
                notes=rationale,
                metadata={"previous_status": previous_status, "new_status": new_status},
            )

            audit_entry = audit_repo.insert_audit_log(
                session=session,
                case_id=case_id,
                entity_type="case",
                entity_id=case_id,
                action="status_updated",
                actor=actor,
                actor_persona=normalize_persona(actor_persona),
                changes={"status": {"old": previous_status, "new": new_status}},
                rationale=rationale,
                metadata={"new_status": new_status},
            )

            updated_case = case_repo.get_case(session, case_id)

            return {
                "case_id": case_id,
                "previous_status": previous_status,
                "new_status": new_status,
                "updated_at": updated_case["updated_at"],
                "timeline_event_id": timeline_event["event_id"],
                "audit_log_id": audit_entry["log_id"],
            }

    def reassign_owner(
        self,
        *,
        case_id: str,
        new_owner: str,
        rationale: str,
        actor: str,
        actor_persona: str,
    ) -> dict[str, Any]:
        """Reassign a case owner."""
        self._assert_persona_allowed(
            actor_persona,
            {"case_owner", "hr_admin"},
            "reassign_case_owner",
        )

        case_repo = HelpCaseRepository()
        timeline_repo = HelpTimelineRepository()
        audit_repo = HelpAuditRepository()

        with get_session() as session:
            case = case_repo.get_case(session, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            self._assert_case_owner_scope(
                actor_persona, actor, case["owner"], "reassign_case_owner"
            )

            previous_owner = case["owner"]
            case_repo.update_owner(session, case_id, new_owner)

            timeline_event = timeline_repo.add_event(
                session=session,
                case_id=case_id,
                event_type="owner_reassigned",
                actor=actor,
                notes=rationale,
                metadata={"previous_owner": previous_owner, "new_owner": new_owner},
            )

            audit_entry = audit_repo.insert_audit_log(
                session=session,
                case_id=case_id,
                entity_type="case",
                entity_id=case_id,
                action="owner_reassigned",
                actor=actor,
                actor_persona=normalize_persona(actor_persona),
                changes={"owner": {"old": previous_owner, "new": new_owner}},
                rationale=rationale,
                metadata={"new_owner": new_owner},
            )

            updated_case = case_repo.get_case(session, case_id)

            return {
                "case_id": case_id,
                "previous_owner": previous_owner,
                "new_owner": new_owner,
                "updated_at": updated_case["updated_at"],
                "timeline_event_id": timeline_event["event_id"],
                "audit_log_id": audit_entry["log_id"],
            }

    def update_due_date(
        self,
        *,
        case_id: str,
        new_due_date: str,
        rationale: str,
        actor: str,
        actor_persona: str,
    ) -> dict[str, Any]:
        """Update the due date for a case."""
        self._assert_persona_allowed(
            actor_persona,
            {"case_owner", "hr_admin"},
            "update_case_due_date",
        )

        case_repo = HelpCaseRepository()
        timeline_repo = HelpTimelineRepository()
        audit_repo = HelpAuditRepository()

        with get_session() as session:
            case = case_repo.get_case(session, case_id)
            if case is None:
                raise ValueError(f"E_CASE_001: Case not found: {case_id}")

            self._assert_case_owner_scope(
                actor_persona, actor, case["owner"], "update_case_due_date"
            )

            previous_due_date = case["due_date"]
            case_repo.update_due_date(session, case_id, new_due_date)

            timeline_event = timeline_repo.add_event(
                session=session,
                case_id=case_id,
                event_type="due_date_updated",
                actor=actor,
                notes=rationale,
                metadata={"previous_due_date": previous_due_date, "new_due_date": new_due_date},
            )

            audit_entry = audit_repo.insert_audit_log(
                session=session,
                case_id=case_id,
                entity_type="case",
                entity_id=case_id,
                action="due_date_updated",
                actor=actor,
                actor_persona=normalize_persona(actor_persona),
                changes={"due_date": {"old": previous_due_date, "new": new_due_date}},
                rationale=rationale,
                metadata={"new_due_date": new_due_date},
            )

            updated_case = case_repo.get_case(session, case_id)

            return {
                "case_id": case_id,
                "previous_due_date": previous_due_date,
                "new_due_date": new_due_date,
                "updated_at": updated_case["updated_at"],
                "timeline_event_id": timeline_event["event_id"],
                "audit_log_id": audit_entry["log_id"],
            }

    def search_cases(
        self,
        *,
        statuses: Iterable[str] | None = None,
        owner: str | None = None,
        candidate_identifier: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        actor_persona: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Search cases with optional filters and pagination."""
        self._assert_persona_allowed(
            actor_persona,
            {"case_owner", "hr_admin", "manager", "hr_analyst"},
            "search_cases",
        )

        owner_filter = owner
        if actor_persona == "case_owner":
            if not actor:
                raise ValueError(
                    "E_AUTH_002: case_owner must supply actor email for scoped searches"
                )
            if owner and owner != actor:
                raise ValueError("E_AUTH_002: case_owner cannot search cases owned by another user")
            owner_filter = actor

        validated_statuses: list[str] | None = None
        if statuses:
            validated_statuses = []
            for status in statuses:
                if not is_valid_status(status):
                    raise ValueError(
                        f"E_CASE_002: Invalid case status. Must be one of: {list(VALID_STATUSES)}"
                    )
                validated_statuses.append(status)

        case_repo = HelpCaseRepository()

        with get_session() as session:
            cases, next_cursor, has_more, used_limit = case_repo.search_cases(
                session=session,
                statuses=validated_statuses,
                owner=owner_filter,
                candidate_identifier=candidate_identifier,
                created_after=created_after,
                created_before=created_before,
                cursor=cursor,
                limit=limit,
            )

            summaries = [
                {
                    "case_id": case["case_id"],
                    "case_type": case["case_type"],
                    "owner": case["owner"],
                    "status": case["status"],
                    "candidate_identifier": case["candidate_identifier"],
                    "due_date": case["due_date"],
                    "created_at": case["created_at"],
                    "updated_at": case["updated_at"],
                }
                for case in cases
            ]

            return {
                "cases": summaries,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": used_limit,
            }
