from __future__ import annotations

import asyncio
import json
from collections import deque

import loguru

from runner.utils.logging.extra_filter import durable_log_extra
from runner.utils.redis import redis_log_client
from runner.utils.settings import get_settings

settings = get_settings()

# Backstop TTL for a stream that's still actively being written to.
_STREAM_TTL_SECONDS = 43200  # 12 hours

# Once a trajectory finishes, its full logs are already durable in Postgres
# (see api_logger.py), so the Redis copy only needs to outlive the async
# API-log drain and any live SSE reader still draining the tail.
_STREAM_DONE_TTL_SECONDS = 120


_MAX_BUFFERED_RECORDS = 10_000
_MAX_BATCH_RECORDS = 1_000
_FLUSH_INTERVAL_SECONDS = 0.25

# Full buffer drops the oldest record: logging must never block the agent.
_buffer: deque[tuple[str, str]] = deque(maxlen=_MAX_BUFFERED_RECORDS)
_dropped_records = 0
_writer: asyncio.Task[None] | None = None
# Held across pop+execute so a teardown flush waits for the writer's in-flight batch.
_flush_lock = asyncio.Lock()


def _enqueue(stream_name: str, payload: str) -> None:
    global _dropped_records
    if len(_buffer) == _MAX_BUFFERED_RECORDS:
        _dropped_records += 1
    _buffer.append((stream_name, payload))


async def flush_redis_logs() -> None:
    """Write buffered records in pipelined batches; a failed batch is dropped."""
    if redis_log_client is None:
        return
    global _dropped_records
    async with _flush_lock:
        if _dropped_records:
            print(
                f"[Redis Logger] Buffer full, dropped {_dropped_records} oldest log records"
            )
            _dropped_records = 0
        # Drain only what is buffered now so a teardown never waits on siblings' new lines.
        pending = len(_buffer)
        while pending > 0 and _buffer:
            batch = [
                _buffer.popleft()
                for _ in range(min(len(_buffer), pending, _MAX_BATCH_RECORDS))
            ]
            pending -= len(batch)
            try:
                async with redis_log_client.pipeline(transaction=False) as pipe:
                    for stream_name, payload in batch:
                        pipe.xadd(stream_name, {"log": payload})
                    for stream_name in {name for name, _ in batch}:
                        pipe.expire(stream_name, _STREAM_TTL_SECONDS)
                    await pipe.execute()
            except Exception as e:
                print(f"[Redis Logger] Dropped {len(batch)} log records: {repr(e)}")


async def _write_loop() -> None:
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
        await flush_redis_logs()


def _ensure_writer() -> None:
    """Start the writer on the calling loop; a writer bound to another loop is replaced."""
    global _writer, _flush_lock
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _writer is not None and not _writer.done() and _writer.get_loop() is loop:
        return
    _flush_lock = asyncio.Lock()
    _writer = loop.create_task(_write_loop())


def redis_sink(message: loguru.Message) -> None:
    record = message.record

    trajectory_id = record["extra"].get("trajectory_id")

    if not trajectory_id or redis_log_client is None:
        return

    log_data = {
        "log_timestamp": record["time"].isoformat(),
        "log_level": record["level"].name,
        "log_message": record["message"],
        "log_extra": durable_log_extra(record["extra"]),
    }

    stream_name = f"{settings.REDIS_STREAM_PREFIX}:{trajectory_id}"

    _enqueue(stream_name, json.dumps(log_data, default=str))
    _ensure_writer()


async def expire_trajectory_stream(trajectory_id: str) -> None:
    """Collapse a trajectory's log stream to a short TTL once its run has
    finished, instead of leaving it on the 12h backstop.

    Best-effort — never raises into the caller, matching the rest of this
    logging package's failure-isolation contract (a Redis blip here must
    never affect the trajectory's own completion).
    """
    if not settings.REDIS_LOGGING or redis_log_client is None:
        return

    stream_name = f"{settings.REDIS_STREAM_PREFIX}:{trajectory_id}"
    await flush_redis_logs()
    try:
        async with _flush_lock:
            await redis_log_client.expire(stream_name, _STREAM_DONE_TTL_SECONDS)
    except Exception as e:
        print(f"[Redis Logger] Failed to expire stream {stream_name}: {repr(e)}")
