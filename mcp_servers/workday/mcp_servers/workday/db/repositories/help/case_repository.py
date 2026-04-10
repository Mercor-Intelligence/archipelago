"""Repository for help case operations using SQLAlchemy.

Converted from raw SQLite to SQLAlchemy ORM pattern.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import HelpCase


def _utc_now_str() -> str:
    """Get current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


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


def _case_to_dict(case: HelpCase) -> dict[str, Any]:
    """Convert HelpCase model to dict."""
    return {
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


class HelpCaseRepository:
    """Repository for help case database operations."""

    def get_case(self, session: Session, case_id: str) -> dict[str, Any] | None:
        """Fetch a case by case_id."""
        case = session.get(HelpCase, case_id)
        if case is None:
            return None
        return _case_to_dict(case)

    def exists_candidate(self, session: Session, candidate_identifier: str) -> bool:
        """Check whether a candidate_identifier already has a case."""
        stmt = (
            select(HelpCase.case_id)
            .where(HelpCase.candidate_identifier == candidate_identifier)
            .limit(1)
        )
        result = session.execute(stmt).first()
        return result is not None

    def create_case(
        self,
        session: Session,
        case_id: str,
        case_type: str,
        owner: str,
        status: str,
        candidate_identifier: str,
        due_date: str | None,
        metadata: dict[str, Any] | None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Create a new help case."""
        now = timestamp or _utc_now_str()
        case = HelpCase(
            case_id=case_id,
            case_type=case_type,
            owner=owner,
            status=status,
            candidate_identifier=candidate_identifier,
            due_date=due_date,
            created_at=now,
            updated_at=now,
            meta=_serialize_metadata(metadata),
        )
        session.add(case)
        session.flush()
        return _case_to_dict(case)

    def update_status(
        self,
        session: Session,
        case_id: str,
        current_status: str,
        new_status: str,
        timestamp: str | None = None,
    ) -> int:
        """Conditionally update case status. Returns number of affected rows."""
        case = session.get(HelpCase, case_id)
        if case is None or case.status != current_status:
            return 0
        case.status = new_status
        case.updated_at = timestamp or _utc_now_str()
        session.flush()
        return 1

    def update_owner(
        self,
        session: Session,
        case_id: str,
        new_owner: str,
        timestamp: str | None = None,
    ) -> int:
        """Update case owner. Returns number of affected rows."""
        case = session.get(HelpCase, case_id)
        if case is None:
            return 0
        case.owner = new_owner
        case.updated_at = timestamp or _utc_now_str()
        session.flush()
        return 1

    def update_due_date(
        self,
        session: Session,
        case_id: str,
        new_due_date: str,
        timestamp: str | None = None,
    ) -> int:
        """Update due date for a case. Returns number of affected rows."""
        case = session.get(HelpCase, case_id)
        if case is None:
            return 0
        case.due_date = new_due_date
        case.updated_at = timestamp or _utc_now_str()
        session.flush()
        return 1

    def search_cases(
        self,
        session: Session,
        statuses: Iterable[str] | None = None,
        owner: str | None = None,
        candidate_identifier: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None, bool, int]:
        """Search cases with optional filters and cursor pagination.

        Returns cases ordered by created_at DESC.
        """
        limit = max(1, min(limit, 200))
        statuses_list = list(statuses) if statuses else []

        stmt = select(HelpCase)

        if statuses_list:
            stmt = stmt.where(HelpCase.status.in_(statuses_list))
        if owner:
            stmt = stmt.where(HelpCase.owner == owner)
        if candidate_identifier:
            stmt = stmt.where(HelpCase.candidate_identifier == candidate_identifier)
        if created_after:
            stmt = stmt.where(HelpCase.created_at >= created_after)
        if created_before:
            stmt = stmt.where(HelpCase.created_at <= created_before)

        if cursor:
            try:
                cursor_created_at, cursor_case_id = cursor.split("|", 1)
                stmt = stmt.where(
                    (HelpCase.created_at < cursor_created_at)
                    | (
                        (HelpCase.created_at == cursor_created_at)
                        & (HelpCase.case_id < cursor_case_id)
                    )
                )
            except ValueError:
                stmt = stmt.where(HelpCase.created_at < cursor)

        stmt = stmt.order_by(HelpCase.created_at.desc(), HelpCase.case_id.desc())
        stmt = stmt.limit(limit + 1)

        rows = session.execute(stmt).scalars().all()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last_case = rows[-1]
            next_cursor = f"{last_case.created_at}|{last_case.case_id}"

        cases = [_case_to_dict(case) for case in rows]
        return cases, next_cursor, has_more, limit
