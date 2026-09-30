"""Compact stdout sink for Modal containers.

The previous stdout sink (``logger.add(sys.stdout, serialize=True)``)
serialized the full record — including multi-hundred-KB extras like model
responses and tool payloads — for every DEBUG line across 16 concurrent
trajectories. That exceeded Modal's per-container output rate limit, so
Modal dropped lines ("exceeded output rate limit"), losing data in both the
Modal viewer and the Datadog ``modal.function-logs`` drain.

Full-fidelity logs already ship through the Datadog API sink (DEBUG,
enqueued), Redis (INFO live view), and the API sink (INFO durable), so
stdout only needs a compact, bounded trail for the Modal viewer.

This sink reads the record and writes its own JSON — it must NOT mutate the
shared loguru record: the async Redis sink and the enqueued Datadog sink may
consume the same record object after this sink runs, and a mutation here
would silently truncate what those full-fidelity channels persist.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

import loguru

# Message body cap. Big enough to keep real context (tracebacks, tool
# summaries), small enough that a burst of model-response dumps stays far
# under Modal's per-container output budget.
_MESSAGE_MAX_CHARS = 2000
# Per-extra-value cap. Identifiers (trajectory_id, function_call_id,
# message_type) pass through untouched; payload-class extras get bounded.
_EXTRA_VALUE_MAX_CHARS = 500
# Formatted tracebacks get their own, larger cap: stack frames are the whole
# point of keeping exceptions in stdout, and they're rare enough not to
# threaten Modal's output budget.
_EXCEPTION_MAX_CHARS = 4000


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...[truncated {len(value) - limit} chars]"


def _truncate_middle(value: str, limit: int) -> str:
    """Truncate the middle, keeping head and tail.

    For tracebacks, head-only truncation cuts the single most important
    line — the trailing ``SomeError: message`` — first. Keep the entry
    frames (head) and the raising frame + exception line (tail).
    """
    if len(value) <= limit:
        return value
    head = int(limit * 0.65)
    tail = limit - head
    dropped = len(value) - head - tail
    return f"{value[:head]}\n...[truncated {dropped} chars]...\n{value[-tail:]}"


def _compact_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Bounded copy of the record's extras; the original is never modified."""
    compact: dict[str, Any] = {}
    for key, value in extra.items():
        if isinstance(value, str):
            compact[key] = _truncate(value, _EXTRA_VALUE_MAX_CHARS)
        else:
            rendered = repr(value)
            compact[key] = (
                value
                if len(rendered) <= _EXTRA_VALUE_MAX_CHARS
                else _truncate(rendered, _EXTRA_VALUE_MAX_CHARS)
            )
    return compact


def truncated_stdout_sink(message: loguru.Message) -> None:
    record = message.record
    exception = record["exception"]
    line: dict[str, Any] = {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "source": f"{record['name']}:{record['function']}:{record['line']}",
        "message": _truncate(record["message"], _MESSAGE_MAX_CHARS),
        "extra": _compact_extra(record["extra"]),
    }
    if exception is not None:
        # Prefer loguru's rendered exception: with the handler registered as
        # format="" + backtrace=True, str(message) is exactly the formatted
        # traceback, including frames above the catch point. Fall back to
        # plain formatting if the handler was registered differently.
        rendered = str(message).strip()
        line["exception"] = _truncate_middle(
            rendered or _format_exception(exception), _EXCEPTION_MAX_CHARS
        )
    sys.stdout.write(json.dumps(line, default=str) + "\n")
    # Modal stdout is block-buffered (PYTHONUNBUFFERED unset); loguru's stream
    # sink flushed per line, so without this sparse lines (e.g. heartbeats)
    # lag in the buffer or vanish when the container is killed.
    sys.stdout.flush()


def _format_exception(exception: Any) -> str:
    """Formatted traceback for the record's exception.

    repr() on loguru's RecordException keeps only type/value and prints the
    traceback as a bare object address — no stack frames. Format it properly
    so Modal stdout stays useful for debugging logger.exception() failures.
    """
    try:
        return "".join(
            traceback.format_exception(
                exception.type, exception.value, exception.traceback
            )
        )
    except Exception:
        return repr(exception)
