from __future__ import annotations

import json

import loguru

from runner.utils.redis import redis_client
from runner.utils.settings import get_settings

settings = get_settings()


async def redis_sink(message: loguru.Message) -> None:
    record = message.record
    extra = record["extra"]

    grading_run_id = extra.get("grading_run_id")
    if not grading_run_id:
        return

    verifier_id = extra.get("verifier_id")
    if isinstance(verifier_id, str) and not verifier_id.strip():
        verifier_id = None

    log_data = {
        "log_timestamp": record["time"].isoformat(),
        "log_level": record["level"].name,
        "log_message": record["message"],
        "log_extra": extra,
        "grading_run_id": grading_run_id,
        "verifier_id": verifier_id,
    }

    stream_name = f"{settings.REDIS_STREAM_PREFIX}:{grading_run_id}"

    await redis_client.xadd(stream_name, {"log": json.dumps(log_data, default=str)})
    await redis_client.expire(stream_name, 43200)  # 12 hours
