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

import loguru

# Matches Studio's `_MAX_FAULT_REASON_CHARS` — more bytes just get trimmed on
# arrival.
_MAX_ERROR_CHARS = 4096

_last_terminal_error: dict[str, str] = {}


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
