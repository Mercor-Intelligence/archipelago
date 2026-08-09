"""Per-trajectory S3 log-part writer (RLS-9434 — trajectory-logs dual-write).

The durable log sink (``api_logger.api_sink``) tees every non-ephemeral record
in here alongside its Postgres write. Records are buffered per ``trajectory_id``;
each full batch (``_PART_SIZE`` events) becomes one gzip-NDJSON *part* object,
PUT to S3 by a single shared background worker via the ``s3_log_parts``
uploader and the server-minted presigned POST policy. At finalization the writer
flushes the remainder and seals a ``manifest.json`` completeness marker.

Design notes
------------
- **State is keyed by ``trajectory_id``.** Under Modal ``@modal.concurrent`` many
  trajectories share one process/event-loop, so a single global buffer/counter
  would interleave prefixes. Everything hangs off ``_state[trajectory_id]``.
- **Single shared worker.** Mirrors ``api_logger``'s one-worker model. Part
  numbers are contiguous per trajectory (assigned when a part is accepted onto
  the FIFO queue), and a per-trajectory finalize marker rides the same queue so
  sealing observes every prior part for that trajectory.
- **Loud, never silent, never fatal.** A shed part (queue full), an upload
  exhaustion, or a refresh failure logs loudly + emits a metric and flags the
  trajectory so its manifest is left *unsealed* — the run keeps going and the
  reconcile sweep rebuilds from Redis. This is the deliberate opposite of
  the old bounded-queue silent drop. Durability never depends on this
  writer: every event is independently in the Redis live-tail stream.
- **Reactive re-mint.** The presign can expire mid-run (its ~6h lifetime is bounded
  by the server's rotating task-role creds), so on an upload/seal *failure* the
  worker re-mints a fresh policy via an injected callback (``register(refresh_fn=…)``,
  so this OSS module never imports internal platform code) and retries once, then
  sheds to the Redis backstop. No proactive expiry tracking.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import loguru

from runner.utils.metrics import increment

from .extra_filter import durable_log_extra
from .s3_log_parts import UploadPolicy, seal_manifest, upload_log_part

# The platform's re-mint callback, injected via register() so this OSS module never
# imports internal modal_helpers. Returns a fresh policy, or None when the server
# declines to mint (flag off).
_RefreshFn = Callable[[str], Awaitable[UploadPolicy | None]]

# Events per part — matches api_logger's HTTP batch size and the shared wire
# contract with the server reader (≤100 events per part).
_PART_SIZE = 100
# Parts in flight before we shed. Generous: the worker drains fast, so this is
# only hit under pathological backpressure, where Redis is the backstop.
_QUEUE_MAXSIZE = 5000
# Bound finalize's wait on the shared worker so one stalled upload can't hang every
# finishing trajectory's teardown in a @modal.concurrent container. Mirrors
# api_logger.teardown_api_logger's 180s bounded drain; on timeout the manifest is
# left unsealed for the reconcile sweep (loss-safe).
_FINALIZE_WAIT_TIMEOUT_SECONDS = 180.0
# After a failed/declined re-mint, back off this long before trying again. A re-mint
# is a server round-trip on the single shared worker's critical path, so a
# persistently failing one must not re-ask on every part (head-of-line blocking).
# Lossless: the failing part just sheds to the Redis backstop until the next re-mint.
_REFRESH_RETRY_COOLDOWN = timedelta(seconds=30)
# Total wall-clock a single re-mint may take on the shared worker. The injected
# callback may retry internally (the server GET is @retry x3), so bound the WHOLE call
# — not just one attempt — to cap head-of-line blocking of co-tenant trajectories'
# uploads. On timeout the part sheds to Redis and the cooldown applies (reconcile
# rebuilds it).
_REMINT_TIMEOUT_SECONDS = 15.0
# Cap on resident per-trajectory state (see register). Backstop for _TrajState leaked
# when a Modal kill/timeout bypasses finalize — far above the @modal.concurrent
# max_inputs=16 ceiling, so it only ever trims genuine leaks, never a live trajectory.
_MAX_TRAJ_STATE = 1024


@dataclass
class _TrajState:
    policy: UploadPolicy
    # Injected platform re-mint callback (None → an expired policy just sheds).
    refresh_fn: _RefreshFn | None = None
    buffer: list[dict[str, Any]] = field(default_factory=list)
    next_part: int = 1
    parts_acked: int = 0
    # A shed/failed log-part write leaves the manifest unsealed (see module
    # docstring); it flags the S3 log write only, never the trajectory's run status.
    had_failure: bool = False
    # Backoff deadline after a failed/declined re-mint (see _REFRESH_RETRY_COOLDOWN).
    refresh_retry_after: datetime | None = None


@dataclass
class _PartItem:
    trajectory_id: str
    part_number: int
    records: list[dict[str, Any]]


@dataclass
class _FinalizeItem:
    trajectory_id: str
    done: asyncio.Event


_WorkItem = _PartItem | _FinalizeItem

_state: dict[str, _TrajState] = {}
_queue: asyncio.Queue[_WorkItem] | None = None
_worker: asyncio.Task[None] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_worker() -> None:
    """Lazily create the shared queue + worker. Called from register(), which
    runs inside the trajectory's event loop."""
    global _queue, _worker
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_worker_loop(), name="trajectory-s3-log-worker")


def register(
    trajectory_id: str,
    policy: UploadPolicy | None,
    refresh_fn: _RefreshFn | None = None,
) -> None:
    """Enable S3 log writing for ``trajectory_id``. No-op when ``policy`` is None
    (S3-write flag off, or an older server that didn't mint one) — the agent then
    silently skips the S3 write.

    ``refresh_fn`` is the platform's re-mint callback, injected here so this OSS
    module never imports internal ``modal_helpers``. The writer calls it *reactively*
    when an upload/seal fails on a (likely expired) policy; when None, a failed write
    just sheds to the Redis backstop."""
    if not policy:
        return
    _ensure_worker()
    # Cap resident state: a trajectory is normally popped at finalize, but a Modal
    # kill/timeout can bypass teardown and leak its _TrajState in a long-lived
    # @modal.concurrent container. Evict the oldest on overflow (F3, best-effort) —
    # its buffered tail is in Redis and the reconcile sweep rebuilds from there.
    if len(_state) >= _MAX_TRAJ_STATE and trajectory_id not in _state:
        del _state[next(iter(_state))]
        increment("studio.trajectory.s3_log_state_evicted")
    _state[trajectory_id] = _TrajState(policy=policy, refresh_fn=refresh_fn)


def is_enabled(trajectory_id: str) -> bool:
    """Whether S3 log writing is active for this trajectory (a policy was
    registered). Lets the log sink skip building the S3 payload on the hot path
    when the flag is off — the common case."""
    return trajectory_id in _state


def append(trajectory_id: str, record: dict[str, Any]) -> None:
    """Buffer one durable log record; flush a part once the buffer is full.
    No-op for unregistered trajectories (policy absent)."""
    state = _state.get(trajectory_id)
    if state is None:
        return
    state.buffer.append(record)
    if len(state.buffer) >= _PART_SIZE:
        _enqueue_part(trajectory_id, state)


async def s3_log_sink(message: loguru.Message) -> None:
    """Durable S3 log sink — a first-class sink alongside ``api_sink``/``redis_sink``
    (registered in ``logging/main.py`` with the same ephemeral filter), not a tee off
    ``api_sink``. No-op unless the server minted an upload policy for this trajectory
    (``is_enabled``), so it stays off the hot path when the S3-write flag is off."""
    record = getattr(message, "record", None)
    if not record:
        return
    trajectory_id = record.get("extra", {}).get("trajectory_id")
    if not trajectory_id or not is_enabled(trajectory_id):
        return
    append(trajectory_id, _durable_log_record(trajectory_id, record))


def _durable_log_record(trajectory_id: str, record: Any) -> dict[str, Any]:
    """Shape one loguru record as the durable ``trajectory_logs`` row the reader
    expects — the same fields the Postgres path persists, so S3 stays byte-parity
    across the S3/PG read paths. Built straight from the record (not by reshaping
    api_logger's HTTP payload). ``trajectory_log_id`` is per-capture; S3 and PG are
    never merged for one trajectory (the reader picks one source), so independent
    ids are fine."""
    extra = durable_log_extra(record["extra"])
    return {
        "trajectory_log_id": f"log_{uuid4().hex}",
        "trajectory_id": trajectory_id,
        "log_timestamp": record["time"].isoformat(),
        "log_message": record["message"],
        "log_level": record["level"].name,
        "log_extra": json.loads(json.dumps(extra, default=str)) if extra else None,
    }


def _enqueue_part(trajectory_id: str, state: _TrajState) -> None:
    """Move the current buffer onto the upload queue as the next part. On a full
    queue, shed loudly and set had_failure (leaves the manifest unsealed)."""
    if _queue is None or not state.buffer:
        return
    records = state.buffer
    state.buffer = []
    item = _PartItem(trajectory_id, state.next_part, records)
    try:
        _queue.put_nowait(item)
    except asyncio.QueueFull:
        state.had_failure = True
        print(
            f"[Trajectory Log S3] Upload queue full, shedding part "
            f"{item.part_number} ({len(records)} events) for {trajectory_id}; "
            f"Redis backstop holds and the reconcile sweep will rebuild it"
        )
        increment("studio.trajectory.s3_log_part_dropped")
        return
    state.next_part += 1


async def _enqueue_and_await_finalize(
    queue: asyncio.Queue[_WorkItem], item: _FinalizeItem
) -> None:
    """Enqueue the finalize marker (blocking put — it must never be shed) and wait for
    the worker to seal. Wrapped by finalize()'s bounded wait_for so a full queue behind
    a stalled worker can't hang teardown past the bound (F1)."""
    await queue.put(item)
    await item.done.wait()


async def finalize(trajectory_id: str) -> None:
    """Flush the trajectory's remaining buffer, then seal its manifest (unless a
    part was shed/failed, in which case it's left unsealed for the sweep). Called
    once per trajectory at teardown; blocks until the worker has drained this
    trajectory's parts + written the manifest."""
    state = _state.get(trajectory_id)
    if state is None or _queue is None:
        return
    queue = _queue  # narrowed non-None; capture for the helper's type
    if state.buffer:
        _enqueue_part(trajectory_id, state)
    item = _FinalizeItem(trajectory_id, asyncio.Event())
    try:
        # Bound the whole enqueue+drain, not just the wait: the blocking put (a full
        # queue behind a stalled worker) is INSIDE the timeout too, so teardown can't
        # hang past the bound. The marker is never shed (blocking put); if we can't even
        # enqueue in time we give up and leave the manifest unsealed for the sweep.
        await asyncio.wait_for(
            _enqueue_and_await_finalize(queue, item),
            timeout=_FINALIZE_WAIT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        print(
            f"[Trajectory Log S3] finalize timed out after "
            f"{_FINALIZE_WAIT_TIMEOUT_SECONDS:.0f}s for {trajectory_id}; manifest may "
            f"be left unsealed (reconcile sweep will seal it)"
        )
        increment("studio.trajectory.s3_log_finalize_timeout")


async def _worker_loop() -> None:
    assert _queue is not None
    while True:
        item = await _queue.get()
        try:
            if isinstance(item, _FinalizeItem):
                await _handle_finalize(item)
            else:
                await _handle_part(item)
        except Exception as exc:  # never let the shared worker die
            print(f"[Trajectory Log S3] Worker item error: {exc!r}")
        finally:
            _queue.task_done()


async def _handle_part(item: _PartItem) -> None:
    state = _state.get(item.trajectory_id)
    if state is None:  # already finalized/cleaned up
        return
    error = await _upload_part(state, item)
    if error is None:
        return
    # Reactive re-mint: the policy may have expired (task-role creds rotate under
    # it). Re-mint once + retry the upload once before shedding.
    if await _remint(state, item.trajectory_id):
        error = await _upload_part(state, item)
        if error is None:
            return
    state.had_failure = True
    print(
        f"[Trajectory Log S3] Failed to upload part {item.part_number} for "
        f"{item.trajectory_id}: {error!r}; Redis backstop holds and the reconcile "
        f"sweep will rebuild it"
    )
    increment("studio.trajectory.s3_log_part_failed")


async def _upload_part(state: _TrajState, item: _PartItem) -> BaseException | None:
    """One upload attempt with the current policy. ``None`` on success (part acked);
    the exception on failure, so the caller can re-mint + retry, then log it."""
    try:
        await upload_log_part(
            state.policy, item.trajectory_id, item.part_number, item.records
        )
        state.parts_acked += 1
        return None
    except Exception as exc:
        return exc


async def _handle_finalize(item: _FinalizeItem) -> None:
    state = _state.pop(item.trajectory_id, None)
    try:
        if state is None:
            return
        if state.had_failure:
            print(
                f"[Trajectory Log S3] Leaving {item.trajectory_id} manifest "
                f"unsealed after a shed/failed part; reconcile sweep will seal it"
            )
            increment("studio.trajectory.s3_log_unsealed")
            return
        error = await _seal(state, item.trajectory_id)
        # The seal can land long after the last part (a quiet tail before the run
        # goes terminal), so its policy may have expired: re-mint once + retry.
        if error is not None and await _remint(state, item.trajectory_id):
            error = await _seal(state, item.trajectory_id)
        if error is None:
            increment("studio.trajectory.s3_log_sealed")
        else:
            print(
                f"[Trajectory Log S3] Failed to seal manifest for "
                f"{item.trajectory_id}: {error!r}; reconcile sweep will seal it"
            )
            increment("studio.trajectory.s3_log_seal_failed")
    finally:
        item.done.set()


async def _seal(state: _TrajState, trajectory_id: str) -> BaseException | None:
    """One seal attempt with the current policy. ``None`` on success; the exception
    on failure."""
    try:
        await seal_manifest(state.policy, trajectory_id, state.parts_acked)
        return None
    except Exception as exc:
        return exc


async def _remint(state: _TrajState, trajectory_id: str) -> bool:
    """Re-mint the presigned policy via the injected callback after a write failed
    (likely an expired policy). Returns True — and swaps in the fresh policy — when a
    re-mint succeeds (caller should retry the write). Each call is bounded to
    _REMINT_TIMEOUT_SECONDS (the injected callback may retry internally) and a failure
    starts a cooldown, so a slow/failing re-mint can't head-of-line-block the shared
    worker; the failing part just sheds to the Redis backstop meanwhile."""
    if state.refresh_fn is None:
        return False
    if state.refresh_retry_after is not None and _now() < state.refresh_retry_after:
        return False  # still backing off from a recent failed re-mint
    try:
        policy = await asyncio.wait_for(
            state.refresh_fn(trajectory_id), timeout=_REMINT_TIMEOUT_SECONDS
        )
    except Exception as exc:
        state.refresh_retry_after = _now() + _REFRESH_RETRY_COOLDOWN
        print(f"[Trajectory Log S3] Re-mint failed for {trajectory_id}: {exc!r}")
        increment("studio.trajectory.s3_log_policy_refresh_failed")
        return False
    if policy:
        state.policy = policy
        state.refresh_retry_after = None
        increment("studio.trajectory.s3_log_policy_refreshed")
        return True
    # Server declined (flag off) — back off, don't re-ask on the next part.
    state.refresh_retry_after = _now() + _REFRESH_RETRY_COOLDOWN
    return False
