"""World reference date ("as-of date") resolution.

A Studio task can pin the world to a fixed calendar day, and an agent reasoning
about a company's valuation on that day must not be handed prices from after it.
The platform publishes the day through the fleet's existing carriers; this module
resolves them in the order the fleet already uses and exposes the two things the
provider layer needs: a cap for ``to_date`` and the instant stamped into
``_meta.timestamp``.

Resolution order (first non-empty, parseable value wins):

1. ``WORLD_ANCHOR_DATE`` — explicit operator override.
2. ``MERCOR_STUDIO_ENV__WORLD_REFERENCE_DATE`` — the prefixed carrier, used
   because arco bakes declared runtime values into ``start.sh`` and would
   otherwise clobber a bare value with an empty string.
3. ``WORLD_REFERENCE_DATE`` — the bare Studio contract variable.
4. ``MERCOR_TASK_CLOCK_DATE`` — informational date the task clock was derived
   from, set when the platform backdates the container.

With none of them set, every function here is a no-op and the server behaves
exactly as it did before: no date cap, real wall-clock timestamps.

Unparseable values fail **open** (warn and ignore) rather than raising. A typo in
an environment variable is something an operator fixes; it must never take a
read-only market-data server down mid-trajectory.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time

from loguru import logger

# Checked in order; first value that parses wins.
REFERENCE_DATE_ENV_VARS = (
    "WORLD_ANCHOR_DATE",
    "MERCOR_STUDIO_ENV__WORLD_REFERENCE_DATE",
    "WORLD_REFERENCE_DATE",
    "MERCOR_TASK_CLOCK_DATE",
)


def _parse(value: str) -> date | None:
    """Parse an ISO date or datetime into a UTC calendar date."""
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def reference_date() -> date | None:
    """Return the world's as-of date, or ``None`` when no anchor is configured."""
    for var in REFERENCE_DATE_ENV_VARS:
        raw = os.getenv(var)
        if raw is None or not raw.strip():
            continue
        parsed = _parse(raw)
        if parsed is not None:
            return parsed
        logger.warning(
            f"Ignoring unparseable {var}={raw!r}: expected an ISO date (YYYY-MM-DD). "
            "Falling back to the real clock."
        )
    return None


def clamp_to_date(to_date: str | None) -> str | None:
    """Cap an upper date bound at the world's as-of date.

    Returns ``to_date`` unchanged when no anchor is set. When one is set, an
    omitted bound becomes the anchor (otherwise the upstream API answers through
    today), and a bound after the anchor is pulled back to it. A bound already at
    or before the anchor is left alone.
    """
    anchor = reference_date()
    if anchor is None:
        return to_date
    if to_date is None:
        return anchor.isoformat()
    requested = _parse(to_date)
    if requested is None:
        # Let the upstream API reject a malformed date with its own message.
        return to_date
    return min(requested, anchor).isoformat()


def response_timestamp() -> str:
    """Return the instant to stamp into ``_meta.timestamp``.

    Pinned to the end of the world's as-of day when an anchor is set, so a
    response does not disclose a date the task has not reached; the real UTC
    clock otherwise.
    """
    anchor = reference_date()
    if anchor is None:
        return datetime.now(UTC).isoformat()
    return datetime.combine(anchor, time.max, tzinfo=UTC).isoformat()


__all__ = [
    "REFERENCE_DATE_ENV_VARS",
    "clamp_to_date",
    "reference_date",
    "response_timestamp",
]
