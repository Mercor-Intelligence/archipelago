from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from uuid import uuid4 as uuid

import asyncpg
import httpx
import loguru

from runner.utils.settings import get_settings

settings = get_settings()

_HTTP_BATCH_SIZE = 100
_HTTP_TIMEOUT_SECONDS = 10.0

_log_queue: asyncio.Queue[dict[str, Any] | None] | None = None
_worker_task: asyncio.Task[None] | None = None
_init_lock: asyncio.Lock | None = None
_conn: asyncpg.Connection | None = None
_stopping: bool = False  # Block new enqueues during shutdown


def _generate_grading_log_id() -> str:
    return f"glog_{uuid().hex}"


def _http_transport_enabled() -> bool:
    return bool(
        settings.GRADING_LOGS_VIA_API
        and settings.RL_STUDIO_API
        and settings.RL_STUDIO_API_KEY
    )


def _to_http_payload(log_data: dict[str, Any]) -> dict[str, Any]:
    return {
        **log_data,
        "log_timestamp": log_data["log_timestamp"].isoformat(),
        "log_extra": json.loads(log_data["log_extra"])
        if log_data["log_extra"]
        else None,
    }


async def _post_log_batch(
    client: httpx.AsyncClient, batch: list[dict[str, Any]]
) -> None:
    payload = {"logs": [_to_http_payload(log_data) for log_data in batch]}
    for attempt in range(2):
        try:
            response = await client.post(
                "/internal/archipelago/webhooks/grading-logs", json=payload
            )
            response.raise_for_status()
            return
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            print(f"[Grading Log API] Error posting {len(batch)} logs: {repr(e)}")


async def _http_log_worker() -> None:
    """
    Drains the queue in batches and ships them to the RL Studio API, so the
    container never holds a direct Postgres connection.
    """
    if _log_queue is None:
        print("[Grading Log API] Queue not initialized")
        return

    try:
        async with httpx.AsyncClient(
            base_url=settings.RL_STUDIO_API or "",
            headers={"X-API-Key": settings.RL_STUDIO_API_KEY or ""},
            timeout=_HTTP_TIMEOUT_SECONDS,
        ) as client:
            print("[Grading Log API] Shipping logs via RL Studio API")
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
                    await _post_log_batch(client, batch)
                finally:
                    for _ in batch:
                        _log_queue.task_done()

                if got_sentinel:
                    break
    except Exception as e:
        print(f"[Grading Log API] Worker error: {repr(e)}")


async def _postgres_log_worker() -> None:
    global _conn, _log_queue

    if not settings.POSTGRES_URL or _log_queue is None:
        print(
            "[Grading Postgres Logger] POSTGRES_URL is not set or queue not initialized"
        )
        return

    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(
            dsn=settings.POSTGRES_URL,
            timeout=10,
            command_timeout=10,
        )
        _conn = conn
        print("[Grading Postgres Logger] Connected with single persistent connection")

        while True:
            try:
                log_data = await _log_queue.get()
            except asyncio.CancelledError:
                break

            if log_data is None:
                break

            if conn is None:
                print("[Grading Postgres Logger] Connection not established")
                continue

            try:
                await conn.execute(
                    """
                    INSERT INTO grading_logs (
                        grading_log_id, grading_run_id, verifier_id,
                        log_timestamp, log_message, log_level, log_extra
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    log_data["grading_log_id"],
                    log_data["grading_run_id"],
                    log_data["verifier_id"],
                    log_data["log_timestamp"],
                    log_data["log_message"],
                    log_data["log_level"],
                    log_data["log_extra"],
                )
            except Exception as e:
                print(f"[Grading Postgres Logger] Error inserting log: {repr(e)}")
            finally:
                _log_queue.task_done()

    except Exception as e:
        print(f"[Grading Postgres Logger] Worker error: {repr(e)}")
    finally:
        try:
            if conn is not None and not conn.is_closed():
                await conn.close()
        except (asyncio.CancelledError, RuntimeError) as e:
            print(
                f"[Grading Postgres Logger] Suppressed close error during shutdown: {repr(e)}"
            )
        except Exception as e:
            print(f"[Grading Postgres Logger] Error during connection close: {repr(e)}")
        finally:
            _conn = None
            print("[Grading Postgres Logger] Connection closed")


async def _log_worker() -> None:
    if _http_transport_enabled():
        await _http_log_worker()
    else:
        await _postgres_log_worker()


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
                _log_worker(), name="grading-postgres-logger-worker"
            )
            print("[Grading Postgres Logger] Started background worker")


async def postgres_sink(message: loguru.Message) -> None:
    global _stopping

    record = getattr(message, "record", None)
    if not record:
        return

    extra = record.get("extra", {})
    grading_run_id = extra.get("grading_run_id")
    if not grading_run_id:
        return

    if not settings.POSTGRES_URL and not _http_transport_enabled():
        return

    if _stopping:
        return

    verifier_id = extra.get("verifier_id")
    if isinstance(verifier_id, str) and not verifier_id.strip():
        verifier_id = None

    try:
        await _ensure_worker_started()

        if _log_queue is None:
            print("[Grading Postgres Logger] Queue not initialized")
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
            print("[Grading Postgres Logger] Queue full, dropping log")

    except Exception as e:
        print(f"[Grading Postgres Logger] Error queuing log: {repr(e)}")


async def teardown_postgres_logger(timeout: float = 180.0) -> None:
    global _stopping, _log_queue, _worker_task

    _stopping = True

    if _log_queue is None or _worker_task is None:
        return

    try:
        with contextlib.suppress(RuntimeError):
            await asyncio.wait_for(_log_queue.join(), timeout=timeout)
    except TimeoutError:
        print(
            f"[Grading Postgres Logger] Queue drain timed out after {timeout}s, forcing shutdown"
        )

    with contextlib.suppress(RuntimeError):
        await _log_queue.put(None)

    try:
        await asyncio.wait_for(_worker_task, timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        print("[Grading Postgres Logger] Worker shutdown timed out, cancelling task")
        _worker_task.cancel()
        with contextlib.suppress(Exception):
            await _worker_task
    finally:
        _worker_task = None
        _log_queue = None
        _stopping = False
