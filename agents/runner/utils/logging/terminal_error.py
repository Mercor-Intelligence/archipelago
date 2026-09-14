"""Capture the last ERROR/CRITICAL log emission per trajectory.

Agents log their terminal exception and then build an ``AgentTrajectoryOutput``
that does not carry it, so a failure reaches Studio with a status and no reason.
Reading it back from ``trajectory_logs`` loses a race it cannot win — completion
is a foreground POST, logging drains from a background queue (measured on dev:
3 of 5 failures). This keeps the reason on the path that already has it.

Mirrors ``final_answer``: same capture-at-log, pop-at-save shape, one sink rather
than editing ``_build_output`` in 48 agents and every agent added after. Keyed by
``trajectory_id`` so trajectories sharing a process under ``@modal.concurrent``
stay isolated, and popped at completion so the store stays bounded.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import loguru

# Matches Studio's `_MAX_FAULT_REASON_CHARS` — more bytes just get trimmed on
# arrival.
_MAX_ERROR_CHARS = 4096

_last_terminal_error: dict[str, str] = {}
_last_terminal_fault: dict[str, dict[str, object]] = {}

# The bind key an agent states its cause under, so Studio classifies from that
# rather than pattern-matching the prose — which it has misread in both
# directions. Build the value with `budget_exhausted()` below, never by hand.
FAULT_BIND_KEY = "fault"


def terminal_error_sink(message: loguru.Message) -> None:
    """Loguru sink: record the latest ERROR/CRITICAL emission per trajectory."""
    record = message.record
    if record["level"].no < loguru.logger.level("ERROR").no:
        return
    # As in the final_answer sink: durable trajectory_logs drops and re-emits
    # ephemeral records, so capturing them would diverge from a log-based reader.
    if record["extra"].get("ephemeral"):
        return
    trajectory_id = record["extra"].get("trajectory_id")
    if not trajectory_id:
        return
    text = (record["message"] or "").strip()
    if not text:
        return
    _last_terminal_error[trajectory_id] = text[:_MAX_ERROR_CHARS]
    # Set together, from THIS record. Writing the fault only when one is bound
    # would leave a later unbound error pairing its prose with an older
    # descriptor — and Studio prefers the descriptor, so the stale one wins.
    fault = record["extra"].get(FAULT_BIND_KEY)
    if isinstance(fault, dict) and fault.get("kind"):
        _last_terminal_fault[trajectory_id] = dict(fault)
    else:
        _last_terminal_fault.pop(trajectory_id, None)


def peek_terminal_error(trajectory_id: str) -> str | None:
    """Read the captured terminal error WITHOUT clearing it.

    For a caller that folds the error onto an output another caller may also
    fold — popping twice would hand the second one None. The owning save path
    still pops, which is what bounds the store.
    """
    return _last_terminal_error.get(trajectory_id)


def pop_terminal_error(trajectory_id: str) -> str | None:
    """Return and clear the last captured terminal error for a trajectory."""
    return _last_terminal_error.pop(trajectory_id, None)


def peek_terminal_fault(trajectory_id: str) -> dict[str, object] | None:
    """`peek_terminal_error` for the structured descriptor."""
    return _last_terminal_fault.get(trajectory_id)


def pop_terminal_fault(trajectory_id: str) -> dict[str, object] | None:
    """`pop_terminal_error` for the structured descriptor."""
    return _last_terminal_fault.pop(trajectory_id, None)


class FaultKind(StrEnum):
    """What went wrong, as the agent knows it. Studio dispatches on this."""

    BUDGET_EXHAUSTED = "budget_exhausted"


class Budget(StrEnum):
    """Which limit ran out. One fault class; this says what to raise."""

    TOKENS = "tokens"
    STEPS = "steps"
    WALL_CLOCK = "wall_clock"


def budget_exhausted(
    budget: Budget, limit: float | int | None = None
) -> dict[str, Any]:
    """Descriptor for a run that hit a limit we imposed.

    Built here, not spelled per call site: an unrecognised `kind` falls back to
    the text by design, so a typo would be silent.
    """
    fault: dict[str, Any] = {
        "kind": FaultKind.BUDGET_EXHAUSTED.value,
        "budget": budget.value,
    }
    if limit is not None:
        fault["limit"] = limit
    return fault
