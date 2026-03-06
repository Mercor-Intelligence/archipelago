from __future__ import annotations

import json

import loguru

from runner.utils.redis import redis_client
from runner.utils.settings import get_settings

settings = get_settings()


async def redis_sink(message: loguru.Message) -> None:
    record = message.record

    trajectory_id = record["extra"].get("trajectory_id")

    if not trajectory_id:
        return

    log_data = {
        "log_timestamp": record["time"].isoformat(),
        "log_level": record["level"].name,
        "log_message": record["message"],
        "log_extra": record["extra"],
    }

    stream_name = f"{settings.REDIS_STREAM_PREFIX}:{trajectory_id}"

    await redis_client.xadd(stream_name, {"log": json.dumps(log_data, default=str)})
    await redis_client.expire(stream_name, 43200)  # 12 hours
