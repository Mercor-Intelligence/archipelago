'"""Helper utilities for managing case status transitions."""'

from __future__ import annotations

from validators.business_rules import STATUS_TRANSITIONS, VALID_STATUSES


def get_allowed_transitions(status: str) -> tuple[str, ...]:
    """Return allowed next statuses for the given current status."""
    return STATUS_TRANSITIONS.get(status, ())


def is_valid_status(status: str) -> bool:
    """Check whether a status value is recognized."""
    return status in VALID_STATUSES


def validate_transition(current: str, new: str, *, code: str = "E_CASE_003") -> None:
    """
    Assert that transitioning from `current` to `new` is permitted by the state machine.

    Raises:
        ValueError: When the requested transition is not allowed.
    """
    allowed = get_allowed_transitions(current)
    if new not in allowed:
        raise ValueError(
            f"{code}: invalid status transition from '{current}' to '{new}', allowed={allowed}"
        )
