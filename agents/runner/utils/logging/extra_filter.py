"""Trajectory-joinable log-extra keys and the durable-sink filter.

agent_id / orchestrator_id (and their SCD versions) are constant per trajectory
and stored on the `trajectories` row, so they are recoverable by joining on
`trajectory_id`. The durable log sinks (Postgres `trajectory_logs`, S3 NDJSON,
the Redis live-tail) therefore exclude them from each row's `log_extra` rather
than persisting a redundant copy on every one of ~2B log rows.

Datadog intentionally does NOT use this filter: its sink forwards the whole
`extra` in the message body and is the one sink that cannot join back to
Postgres, so it is where per-step agent attribution actually lives.
"""

from typing import Any

DURABLE_EXTRA_EXCLUDE_KEYS = frozenset(
    {"agent_id", "orchestrator_id", "agent_version", "orchestrator_version"}
)


def durable_log_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Copy of a log record's `extra` minus the trajectory-joinable attribution
    keys. Never mutates the original — other sinks still see the full dict."""
    if not extra:
        return extra
    return {k: v for k, v in extra.items() if k not in DURABLE_EXTRA_EXCLUDE_KEYS}
