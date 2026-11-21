from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, final

import loguru

from runner.utils.logging.extra_filter import (
    durable_log_extra,
    resolve_trajectory_log_id,
)
from runner.utils.metrics import increment
from runner.utils.settings import get_settings
from runner.utils.studio_http import studio_post_json

settings = get_settings()

# Each POST becomes at least one S3 object per trajectory; lines come in clumps a few ms apart (STPL-510).
_LINGER_SECONDS = 1.0
# The webhook rejects more (TrajectoryLogsWebhookRequest.logs), and a 422 is not retried.
_BATCH_MAX_RECORDS = 1000
# The server splits an S3 object at 10 MiB of NDJSON.
_BATCH_MAX_BYTES = 10 * 1024 * 1024
_RECORD_OVERHEAD_BYTES = 256
_HTTP_TIMEOUT_SECONDS = 30.0
_QUEUE_FULL_WAIT_SECONDS = 30.0
# Put by teardown: send what is buffered now and stop lingering.
_FLUSH = {"flush": True}
# When api_sink queued the record (loop clock); stripped from the POST. Agents backdate log_timestamp.
_QUEUED_AT = "_queued_at"

_log_queue: asyncio.Queue[dict[str, Any] | None] | None = None
_worker_task: asyncio.Task[None] | None = None
_init_lock: asyncio.Lock | None = None
_stopping: bool = False


def _api_enabled() -> bool:
    return bool(settings.RL_STUDIO_API and settings.RL_STUDIO_API_KEY)


def _to_http_payload(log_data: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in log_data.items() if key != _QUEUED_AT},
        "log_timestamp": log_data["log_timestamp"].isoformat(),
        "log_extra": json.loads(log_data["log_extra"])
        if log_data["log_extra"]
        else None,
    }


async def _post_log_batch(batch: list[dict[str, Any]]) -> None:
    payload = {"logs": [_to_http_payload(log_data) for log_data in batch]}
    url = f"{settings.RL_STUDIO_API}/internal/archipelago/webhooks/trajectory-logs"
    try:
        await studio_post_json(
            url,
            payload,
            {"X-API-Key": settings.RL_STUDIO_API_KEY or ""},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except Exception as e:
        # Loud, not silent: the batch is lost from the Postgres path. The Redis
        # live-tail may still carry these events, but it is best-effort too.
        print(
            f"[Trajectory Log API] Error posting {len(batch)} logs "
            f"(Redis backstop): {repr(e)}"
        )
        increment("studio.trajectory.pg_log_batch_failed")


def _valid_utf8(text: str) -> str:
    """``text`` with lone surrogates replaced: httpx encodes the body as strict UTF-8, so one drops its batch."""
    return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def _record_bytes(log_data: dict[str, Any]) -> int:
    """Roughly the record's share of the POST body; only the batch budget reads it."""
    message: str = log_data["log_message"]
    extra: str | None = log_data["log_extra"]
    # surrogatepass: api_sink already replaced them; a stray one must not stop the worker.
    return (
        len(message.encode("utf-8", "surrogatepass"))
        + len((extra or "").encode("utf-8", "surrogatepass"))
        + _RECORD_OVERHEAD_BYTES
    )


@final
class _Batch:
    """Lines not yet posted, sent ``_LINGER_SECONDS`` after the first of them was queued."""

    def __init__(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._queue = queue
        self.records: list[dict[str, Any]] = []
        self.bytes = 0
        self.deadline = 0.0

    def fits(self, size: int) -> bool:
        return not self.records or (
            len(self.records) < _BATCH_MAX_RECORDS
            and self.bytes + size <= _BATCH_MAX_BYTES
        )

    def add(self, log_data: dict[str, Any], size: int, now: float) -> None:
        if not self.records:
            # From when the line was queued, not when the worker took it: a line that
            # waited out a slow POST is already due.
            queued: float = log_data.get(_QUEUED_AT, now)
            self.deadline = min(queued, now) + _LINGER_SECONDS
        self.records.append(log_data)
        self.bytes += size

    def full(self) -> bool:
        return len(self.records) >= _BATCH_MAX_RECORDS or self.bytes >= _BATCH_MAX_BYTES

    async def send(self) -> None:
        if not self.records:
            return
        records, self.records, self.bytes = self.records, [], 0
        try:
            await _post_log_batch(records)
        finally:
            # After the POST, so teardown's join() waits until every line is sent.
            for _ in records:
                self._queue.task_done()


async def _api_log_worker() -> None:
    """Ship queued logs in batches, one POST at a time; see ``_Batch`` for when a batch goes."""
    if _log_queue is None:
        print("[Trajectory Log API] Queue not initialized")
        return

    queue = _log_queue
    loop = asyncio.get_running_loop()
    batch = _Batch(queue)
    lingering = True
    print("[Trajectory Log API] Shipping logs via RL Studio API")
    try:
        while True:
            # Keep draining the queue while a batch waits, so producers never block on it.
            wait = max(0.0, batch.deadline - loop.time()) if lingering else 0.0
            try:
                if not batch.records:
                    log_data = await queue.get()
                elif wait > 0:
                    log_data = await asyncio.wait_for(queue.get(), wait)
                else:
                    log_data = queue.get_nowait()
            except (TimeoutError, asyncio.QueueEmpty):
                await batch.send()
                continue
            except asyncio.CancelledError:
                break

            if log_data is None:
                await batch.send()
                break
            if log_data is _FLUSH:
                queue.task_done()
                lingering = False
                await batch.send()
                continue

            size = _record_bytes(log_data)
            if not batch.fits(size):
                await batch.send()
            batch.add(log_data, size, loop.time())
            if batch.full():
                await batch.send()
    except Exception as e:
        print(f"[Trajectory Log API] Worker error: {repr(e)}")


async def _ensure_worker_started() -> None:
    global _log_queue, _worker_task, _init_lock

    if _init_lock is None:
        _init_lock = asyncio.Lock()

    if _log_queue is not None and _worker_task is not None and not _worker_task.done():
        return

    async with _init_lock:
        if _log_queue is None:
            _log_queue = asyncio.Queue(maxsize=1000)

        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(
                _api_log_worker(), name="trajectory-api-logger-worker"
            )
            print("[Trajectory Log API] Started background worker")


async def api_sink(message: loguru.Message) -> None:
    """Queue a log message to be persisted via the RL Studio internal API."""
    global _stopping

    record = getattr(message, "record", None)
    if not record:
        return

    trajectory_id = record.get("extra", {}).get("trajectory_id")
    if not trajectory_id:
        return

    if not settings.API_LOGGING or not _api_enabled():
        return

    if _stopping:
        return

    try:
        await _ensure_worker_started()

        if _log_queue is None:
            print("[Trajectory Log API] Queue not initialized")
            return

        log_data = {
            # Minted once at capture by the patcher (RLS-9809); a local mint is counted.
            "trajectory_log_id": resolve_trajectory_log_id(record),
            "trajectory_id": trajectory_id,
            "log_timestamp": record["time"],
            "log_message": _valid_utf8(record["message"]),
            "log_level": record["level"].name,
            # ensure_ascii=False, or an escaped surrogate comes back when the POST decodes it.
            "log_extra": _valid_utf8(
                json.dumps(
                    durable_log_extra(record["extra"]), default=str, ensure_ascii=False
                )
            ),
            _QUEUED_AT: asyncio.get_running_loop().time(),
        }

        try:
            _log_queue.put_nowait(log_data)
        except asyncio.QueueFull:
            # Lighthouse logs a whole run in one burst at the end; wait for the drain
            # instead of dropping every row past the queue's 1,000.
            try:
                await asyncio.wait_for(
                    _log_queue.put(log_data), timeout=_QUEUE_FULL_WAIT_SECONDS
                )
            except TimeoutError:
                # Loud, not silent: the Postgres queue overflowed, but the
                # event is still in the Redis live-tail.
                print(
                    "[Trajectory Log API] Postgres log queue full, dropping log "
                    "(Redis backstop)"
                )
                increment("studio.trajectory.pg_log_dropped")

    except Exception as e:
        print(f"[Trajectory Log API] Error queuing log: {repr(e)}")


async def teardown_api_logger(timeout: float = 180.0) -> None:
    """Flush pending logs and shut down the worker cleanly."""
    global _stopping, _log_queue, _worker_task

    _stopping = True

    if _log_queue is None or _worker_task is None:
        return

    try:
        with contextlib.suppress(RuntimeError):
            async with asyncio.timeout(timeout):
                # Without it, the last lines would wait out the linger before teardown ends.
                await _log_queue.put(_FLUSH)
                await _log_queue.join()
    except TimeoutError:
        print(
            f"[Trajectory Log API] Queue drain timed out after {timeout}s, forcing shutdown"
        )

    with contextlib.suppress(RuntimeError):
        await _log_queue.put(None)

    try:
        await asyncio.wait_for(_worker_task, timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        print("[Trajectory Log API] Worker shutdown timed out, cancelling task")
        _worker_task.cancel()
        with contextlib.suppress(Exception):
            await _worker_task
    finally:
        _worker_task = None
        _log_queue = None
        _stopping = False
