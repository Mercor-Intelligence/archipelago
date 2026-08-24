from __future__ import annotations

import json

import loguru

from runner.utils.logging.extra_filter import durable_log_extra
from runner.utils.redis import redis_client
from runner.utils.settings import get_settings

settings = get_settings()

# Backstop TTL for a stream that's still actively being written to.
_STREAM_TTL_SECONDS = 43200  # 12 hours

# Once a trajectory finishes, its full logs are already durable in Postgres
# (see api_logger.py), so the Redis copy only needs to outlive the async
# API-log drain and any live SSE reader still draining the tail.
_STREAM_DONE_TTL_SECONDS = 120


async def redis_sink(message: loguru.Message) -> None:
    record = message.record

    trajectory_id = record["extra"].get("trajectory_id")

    if not trajectory_id or redis_client is None:
        return

    log_data = {
        "log_timestamp": record["time"].isoformat(),
        "log_level": record["level"].name,
        "log_message": record["message"],
        "log_extra": durable_log_extra(record["extra"]),
    }

    stream_name = f"{settings.REDIS_STREAM_PREFIX}:{trajectory_id}"

    await redis_client.xadd(stream_name, {"log": json.dumps(log_data, default=str)})
    await redis_client.expire(stream_name, _STREAM_TTL_SECONDS)


async def expire_trajectory_stream(trajectory_id: str) -> None:
    """Collapse a trajectory's log stream to a short TTL once its run has
    finished, instead of leaving it on the 12h backstop.

    Best-effort — never raises into the caller, matching the rest of this
    logging package's failure-isolation contract (a Redis blip here must
    never affect the trajectory's own completion).
    """
    if not settings.REDIS_LOGGING or redis_client is None:
        return

    stream_name = f"{settings.REDIS_STREAM_PREFIX}:{trajectory_id}"
    try:
        await redis_client.expire(stream_name, _STREAM_DONE_TTL_SECONDS)
    except Exception as e:
        print(f"[Redis Logger] Failed to expire stream {stream_name}: {repr(e)}")
