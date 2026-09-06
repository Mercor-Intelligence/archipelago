"""Trajectory-joinable log-extra keys, the durable-sink filter, and the durable row id.

agent_id / orchestrator_id (and their SCD versions) are constant per trajectory
and stored on the `trajectories` row, so they are recoverable by joining on
`trajectory_id`. The durable log sinks (Postgres `trajectory_logs`, S3 NDJSON,
the Redis live-tail) therefore exclude them from each row's `log_extra` rather
than persisting a redundant copy on every one of ~2B log rows.

Datadog intentionally does NOT use this filter: its sink forwards the whole
`extra` in the message body and is the one sink that cannot join back to
Postgres, so it is where per-step agent attribution actually lives.

`trajectory_log_id` lives here too, because it is minted into `extra` (see
`stamp_trajectory_log_id`) and then excluded from `log_extra` by the same filter.
"""

from typing import Any
from uuid import uuid4

# Where the patcher parks this line's durable row id, for every sink to read.
TRAJECTORY_LOG_ID_KEY = "trajectory_log_id"

DURABLE_EXTRA_EXCLUDE_KEYS = frozenset(
    {
        "agent_id",
        "orchestrator_id",
        "agent_version",
        "orchestrator_version",
        # Already a top-level column/field on every durable row, and the sinks read it
        # off `extra` rather than persisting it there. Excluded so the id does not also
        # appear inside `log_extra`, which would put a redundant copy on ~2B rows and
        # diverge the payload between agents that stamp it and agents that don't.
        TRAJECTORY_LOG_ID_KEY,
    }
)


def durable_log_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Copy of a log record's `extra` minus the trajectory-joinable attribution
    keys. Never mutates the original — other sinks still see the full dict."""
    if not extra:
        return extra
    return {k: v for k, v in extra.items() if k not in DURABLE_EXTRA_EXCLUDE_KEYS}


def mint_trajectory_log_id() -> str:
    """A fresh durable-log-row id. One definition of the format, in one place."""
    return f"log_{uuid4().hex}"


# Reported once per process, not per line: whether the patcher is installed is a
# process-wide fact, and per-line emission would put two Datadog submissions on every
# record in exactly the degraded state it reports. Unlocked — one event loop, and a lost
# race costs a duplicate point, not a missed alert.
_unstamped_reported = False


def resolve_trajectory_log_id(record: Any) -> str:
    """This line's id: the one the patcher minted at capture, or a fresh local mint.

    The fallback keeps a record from being written with no id, but it also means the two
    durable sinks have silently gone back to minting independently — the exact divergence
    this exists to prevent, and one that cannot be repaired after the fact. So it is
    counted. Reachable if the patcher stops running: the ``logger.configure`` call dropped
    in a refactor, an agent entrypoint that adds the durable sinks without going through
    ``setup_logger``, or the vendored agent_sandbox replica drifting from this tree.
    """
    global _unstamped_reported
    existing = record["extra"].get(TRAJECTORY_LOG_ID_KEY)
    if existing:
        return existing
    if not _unstamped_reported:
        _unstamped_reported = True
        # Deferred so importing this module — and therefore logging/main.py — does not
        # pull in datadog_api_client or resolve Settings at import time. Matches the
        # lazy-sink convention in logging/main.py, and runs at most once per process.
        from runner.utils.metrics import increment  # import-check-ignore

        increment("studio.trajectory.log_id_unstamped")
    return mint_trajectory_log_id()


def stamp_trajectory_log_id(record: Any) -> None:
    """Loguru patcher: mint this line's `trajectory_log_id` ONCE, at capture (RLS-9809).

    The Postgres sink and the S3 sink are independent loguru sinks that share no state,
    so each used to mint its own id for the same captured line. That made the two stores
    disagree about the id of the same row, which breaks two things:

    - `get_trajectory_log(trajectory_id, log_id)` is documented as "use after
      `list_trajectory_logs`". The reader picks its source per *read*, not per trajectory
      (one transient S3 error serves read 1 from S3 and read 2 from Postgres), so a
      caller can hand back an id the other store has never heard of.
    - ordering equal-`log_timestamp` rows by `(log_timestamp, trajectory_log_id)` — the
      natural fix for S3 and Postgres returning ties in different orders — cannot work
      while the tiebreaker column differs per store.

    Stamping at capture is what makes the id shared: it must NOT come from either sink,
    because the S3 sink is gated on its own toggle specifically so it keeps working once
    Postgres logging is off. Sinks fall back to minting locally if the key is absent,
    which degrades to the old behaviour rather than writing a row with no id.

    Only lines carrying a `trajectory_id` are stamped — the durable sinks all no-op
    without one, so framework noise and stdout-only lines don't pay for a uuid.
    """
    extra = record["extra"]
    if extra.get("trajectory_id") and TRAJECTORY_LOG_ID_KEY not in extra:
        extra[TRAJECTORY_LOG_ID_KEY] = mint_trajectory_log_id()
