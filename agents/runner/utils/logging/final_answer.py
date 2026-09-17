"""Capture the last ``message_type='final_answer'`` log emission per trajectory.

Agents across the runner emit their final answer as a structured log line
(``logger.bind(message_type="final_answer").info(<answer>)``). This sink
snapshots the most recent such emission per trajectory into an in-memory store
so the runner can denormalize it onto the trajectory row at completion (RLS-9433)
— without a consumer re-scanning the full transcript or (post-offload) fetching
it from S3.

Keyed by ``trajectory_id`` (bound via ``logger.contextualize`` in
``runner/main.py``), so trajectories sharing one process under Modal
``@modal.concurrent`` stay isolated. The entry is popped at completion, so the
store is bounded by the number of concurrently in-flight trajectories.
"""

from __future__ import annotations

import loguru

# trajectory_id -> last captured final_answer message.
_last_final_answer: dict[str, str] = {}


def final_answer_sink(message: loguru.Message) -> None:
    """Loguru sink: record the latest final_answer emission per trajectory."""
    record = message.record
    if record["extra"].get("message_type") != "final_answer":
        return
    # Mirror the durable api_sink's ephemeral filter (see setup_logger): ephemeral
    # records are live-stream telemetry the durable trajectory_logs deliberately
    # drops and re-emits later. Skipping them here keeps the captured value
    # byte-identical to what the log-based reader returns, which is the invariant
    # the S3 read cutover (PR5) parity-checks against.
    if record["extra"].get("ephemeral"):
        return
    trajectory_id = record["extra"].get("trajectory_id")
    if not trajectory_id:
        return
    _last_final_answer[trajectory_id] = record["message"]


def pop_final_answer(trajectory_id: str) -> str | None:
    """Return and clear the last captured final answer for a trajectory."""
    return _last_final_answer.pop(trajectory_id, None)
