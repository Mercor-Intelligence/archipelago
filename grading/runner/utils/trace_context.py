"""One trace id for a unit of Studio work, carried on every hop it makes.

A unit is one job: a trajectory (its agent run and its grading share the trajectory's
id), or a pipeline run. Its trace id is a pure function of that id, so nothing has to
be stored or threaded: any process that knows which job it is working for derives the
same trace id, including for jobs that ran before this module existed.

The Modal runners do not run a tracer, so they send the trace as plain headers, in
the two forms our services read:

- Datadog's own headers, for the Studio server and the LLM Gateway, which run
  Datadog's tracer. They carry no sampling decision, so each service keeps applying
  its own sampling rate, exactly as it does today.
- The W3C ``traceparent`` header, for the LiteLLM proxy, which traces with
  OpenTelemetry and reads only that. A ``traceparent`` must say "sampled" or "not
  sampled"; it says "sampled", which is what the proxy already does for every request.

Datadog's tracer reads its own headers first and ignores the ``traceparent`` flag when
both are present, so sending the second form does not change what it keeps.

Off switch: set ``STUDIO_TRACE_DISABLED=1`` in a process's environment and it sends no
trace headers and stamps no trace log fields. Read on every call, so a new container
picks it up without a code change.

Stdlib only. The runners keep byte-identical copies (``scripts/vendor_trace_context.py``).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from functools import lru_cache

# Log field holding the full 32-hex trace id; the one to search by.
TRACE_ID_LOG_KEY = "trace_id"
# Log field Datadog reads to link a log line to its trace (low 64 bits, decimal).
DD_TRACE_ID_LOG_KEY = "dd.trace_id"

DD_TRACE_ID_HEADER = "x-datadog-trace-id"
DD_PARENT_ID_HEADER = "x-datadog-parent-id"
DD_TAGS_HEADER = "x-datadog-tags"
W3C_TRACEPARENT_HEADER = "traceparent"

# Set to 1 / true / yes to send no trace headers and stamp no trace log fields.
DISABLE_ENV = "STUDIO_TRACE_DISABLED"

_LOW_64 = (1 << 64) - 1
_LOW_63 = (1 << 63) - 1


@lru_cache(maxsize=4096)
def _ids(unit_id: str) -> tuple[int, int, int]:
    """(high 64 bits, low 64 bits, parent span id) for a unit. Low and parent are never 0."""
    digest = hashlib.sha256(b"studio-trace:" + unit_id.encode("utf-8")).digest()
    high = int.from_bytes(digest[:8], "big")
    # Datadog treats a zero id as "no trace", so a zero half is nudged to 1.
    low = int.from_bytes(digest[8:16], "big") or 1
    parent = (int.from_bytes(digest[16:24], "big") & _LOW_63) or 1
    return high, low, parent


def trace_disabled() -> bool:
    """True when this process's off switch is set."""
    # RLS002 waiver: this file is copied byte-for-byte into runners that have no Settings.
    value = os.environ.get(DISABLE_ENV, "")  # noqa: RLS002
    return value.strip().lower() in {"1", "true", "yes"}


def trace_id_for(unit_id: str) -> str:
    """The unit's trace id: 32 lowercase hex characters."""
    high, low, _ = _ids(unit_id)
    return f"{high:016x}{low:016x}"


def trace_headers(unit_id: str | None) -> dict[str, str]:
    """Headers that make the receiving service continue this unit's trace. Empty without a unit."""
    if not unit_id or trace_disabled():
        return {}
    high, low, parent = _ids(unit_id)
    return {
        DD_TRACE_ID_HEADER: str(low),
        DD_PARENT_ID_HEADER: str(parent),
        DD_TAGS_HEADER: f"_dd.p.tid={high:016x}",
        W3C_TRACEPARENT_HEADER: f"00-{high:016x}{low:016x}-{parent:016x}-01",
    }


def with_trace_headers(
    headers: Mapping[str, str] | None, unit_id: str | None
) -> dict[str, str]:
    """A copy of ``headers`` plus the trace headers.

    A caller that already set a trace keeps it whole: if any trace header is present
    (in any letter case) none is added, so two traces are never mixed in one request.
    """
    merged = dict(headers or {})
    wanted = trace_headers(unit_id)
    if not wanted:
        return merged
    present = {name.lower() for name in merged}
    if present.isdisjoint(wanted):
        merged.update(wanted)
    return merged


def trace_log_fields(unit_id: str | None) -> dict[str, str]:
    """Fields to put on a log line so it can be found by trace id. Empty without a unit."""
    if not unit_id or trace_disabled():
        return {}
    high, low, _ = _ids(unit_id)
    return {
        TRACE_ID_LOG_KEY: f"{high:016x}{low:016x}",
        DD_TRACE_ID_LOG_KEY: str(low),
    }
