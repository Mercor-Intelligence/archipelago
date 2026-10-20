from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from uuid import uuid4 as uuid

import loguru

from runner.utils.settings import get_settings
from runner.utils.studio_http import studio_post_json

settings = get_settings()

_HTTP_BATCH_SIZE = 100
_HTTP_TIMEOUT_SECONDS = 10.0

_log_queue: asyncio.Queue[dict[str, Any] | None] | None = None
_worker_task: asyncio.Task[None] | None = None
_init_lock: asyncio.Lock | None = None
_stopping: bool = False


def _generate_grading_log_id() -> str:
    return f"glog_{uuid().hex}"


def _api_enabled() -> bool:
    return bool(settings.RL_STUDIO_API and settings.RL_STUDIO_API_KEY)


def _to_http_payload(log_data: dict[str, Any]) -> dict[str, Any]:
    return {
        **log_data,
        "log_timestamp": log_data["log_timestamp"].isoformat(),
        "log_extra": json.loads(log_data["log_extra"])
        if log_data["log_extra"]
        else None,
    }


async def _post_log_batch(batch: list[dict[str, Any]]) -> None:
    payload = {"logs": [_to_http_payload(log_data) for log_data in batch]}
    url = f"{settings.RL_STUDIO_API}/internal/archipelago/webhooks/grading-logs"
    try:
        await studio_post_json(
            url,
            payload,
            {"X-API-Key": settings.RL_STUDIO_API_KEY or ""},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except Exception as e:
        print(f"[Grading Log API] Error posting {len(batch)} logs: {repr(e)}")


async def _api_log_worker() -> None:
    """Drain the queue in batches and ship logs to the RL Studio API."""
    if _log_queue is None:
        print("[Grading Log API] Queue not initialized")
        return

    if _log_queue is None:
        print("[Grading Log API] Queue not initialized")
        return

    print("[Grading Log API] Shipping logs via RL Studio API")
    try:
        while True:
            try:
                log_data = await _log_queue.get()
            except asyncio.CancelledError:
                break

            if log_data is None:
                break

            batch = [log_data]
            got_sentinel = False
            while len(batch) < _HTTP_BATCH_SIZE:
                try:
                    next_log = _log_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_log is None:
                    got_sentinel = True
                    break
                batch.append(next_log)

            try:
                await _post_log_batch(batch)
            finally:
                for _ in batch:
                    _log_queue.task_done()

            if got_sentinel:
                break
    except Exception as e:
        print(f"[Grading Log API] Worker error: {repr(e)}")


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
                _api_log_worker(), name="grading-api-logger-worker"
            )
            print("[Grading Log API] Started background worker")


async def api_sink(message: loguru.Message) -> None:
    """Queue a log message to be persisted via the RL Studio internal API."""
    global _stopping

    record = getattr(message, "record", None)
    if not record:
        return

    extra = record.get("extra", {})
    grading_run_id = extra.get("grading_run_id")
    if not grading_run_id:
        return

    if not settings.API_LOGGING or not _api_enabled():
        return

    if _stopping:
        return

    verifier_id = extra.get("verifier_id")
    if isinstance(verifier_id, str) and not verifier_id.strip():
        verifier_id = None

    try:
        await _ensure_worker_started()

        if _log_queue is None:
            print("[Grading Log API] Queue not initialized")
            return

        log_data = {
            "grading_log_id": _generate_grading_log_id(),
            "grading_run_id": grading_run_id,
            "verifier_id": verifier_id,
            "log_timestamp": record["time"],
            "log_message": record["message"],
            "log_level": record["level"].name,
            "log_extra": json.dumps(extra, default=str),
        }

        try:
            _log_queue.put_nowait(log_data)
        except asyncio.QueueFull:
            print("[Grading Log API] Queue full, dropping log")

    except Exception as e:
        print(f"[Grading Log API] Error queuing log: {repr(e)}")


async def teardown_api_logger(timeout: float = 180.0) -> None:
    """Flush pending logs and shut down the worker cleanly."""
    global _stopping, _log_queue, _worker_task

    _stopping = True

    if _log_queue is None or _worker_task is None:
        return

    try:
        with contextlib.suppress(RuntimeError):
            await asyncio.wait_for(_log_queue.join(), timeout=timeout)
    except TimeoutError:
        print(
            f"[Grading Log API] Queue drain timed out after {timeout}s, forcing shutdown"
        )

    with contextlib.suppress(RuntimeError):
        await _log_queue.put(None)

    try:
        await asyncio.wait_for(_worker_task, timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        print("[Grading Log API] Worker shutdown timed out, cancelling task")
        _worker_task.cancel()
        with contextlib.suppress(Exception):
            await _worker_task
    finally:
        _worker_task = None
        _log_queue = None
        _stopping = False
