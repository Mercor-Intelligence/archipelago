"""Business rule validators for Workday Help MCP schemas."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal, get_args

CaseTypeLiteral = Literal["Pre-Onboarding"]
VALID_CASE_TYPES = get_args(CaseTypeLiteral)

StatusLiteral = Literal["Open", "Waiting", "In Progress", "Resolved", "Closed"]
VALID_STATUSES = get_args(StatusLiteral)

DirectionLiteral = Literal["internal", "inbound", "outbound"]
VALID_DIRECTIONS = get_args(DirectionLiteral)

EventTypeLiteral = Literal[
    "case_created",
    "status_changed",
    "owner_reassigned",
    "due_date_updated",
    "message_added",
    "attachment_added",
    "decision_logged",
]
VALID_EVENT_TYPES = get_args(EventTypeLiteral)

AudienceLiteral = Literal["candidate", "hiring_manager", "recruiter", "internal_hr"]
VALID_AUDIENCES = get_args(AudienceLiteral)

PersonaLiteral = Literal["case_owner", "hr_admin", "manager", "hr_analyst"]
SUPPORTED_PERSONAS = set(get_args(PersonaLiteral))

# Map persona slugs to human-readable display names for audit logs
PERSONA_DISPLAY_MAP: dict[str, str] = {
    "case_owner": "Case Owner",
    "hr_admin": "HR Admin",
    "manager": "Manager",
    "hr_analyst": "HR Analyst",
}


def normalize_persona(persona: str) -> str:
    """Normalize persona slug to human-friendly name for audit logs.

    Args:
        persona: Input persona slug (case_owner, hr_admin, manager, hr_analyst)

    Returns:
        Human-readable persona name (Case Owner, HR Admin, Manager, HR Analyst)
        Falls back to input if not found in map.
    """
    return PERSONA_DISPLAY_MAP.get(persona, persona)


STATUS_TRANSITIONS = {
    "Open": ("Waiting", "In Progress"),
    "Waiting": ("In Progress", "Resolved"),
    "In Progress": ("Resolved",),
    "Resolved": ("Closed",),
    "Closed": (),
}


def ensure_enum(value: str, allowed: Iterable[str], code: str = "E_VAL_001") -> str:
    if value not in allowed:
        raise ValueError(f"{code}: invalid value '{value}', allowed={list(allowed)}")
    return value


def ensure_future_iso8601(value: str | None, field_name: str) -> str | None:
    if value is None:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - clarity
        raise ValueError(f"E_VAL_001: {field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed <= datetime.now(UTC):
        raise ValueError(f"E_VAL_002: {field_name} must be in the future")
    return value


def ensure_valid_transition(current: str, new: str) -> None:
    allowed = STATUS_TRANSITIONS.get(current, ())
    if new not in allowed:
        raise ValueError(
            f"E_CASE_003: invalid status transition from '{current}' to '{new}', allowed={allowed}"
        )


def validate_date_range(start: str | None, end: str | None, field_label: str) -> None:
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"E_VAL_001: {field_label} must be ISO 8601") from exc
        if start_dt > end_dt:
            raise ValueError(f"E_VAL_002: {field_label} start must be <= end")
