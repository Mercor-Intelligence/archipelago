from __future__ import annotations

import json

import loguru
from datadog_api_client import Configuration, ThreadedApiClient
from datadog_api_client.v2.api.logs_api import LogsApi
from datadog_api_client.v2.model.http_log import HTTPLog
from datadog_api_client.v2.model.http_log_item import HTTPLogItem
from loguru import logger

from runner.utils.settings import get_settings

settings = get_settings()

if not settings.DATADOG_API_KEY or not settings.DATADOG_APP_KEY:
    raise ValueError(
        "DATADOG_API_KEY and DATADOG_APP_KEY must be set to use the Datadog logger"
    )

configuration = Configuration()
configuration.api_key["apiKeyAuth"] = settings.DATADOG_API_KEY
configuration.api_key["appKeyAuth"] = settings.DATADOG_APP_KEY

api_client = ThreadedApiClient(configuration)


def datadog_sink(message: loguru.Message):
    """Send logs to Datadog."""
    record = message.record

    try:
        tags = {
            "env": settings.ENV.value,
            "trajectory_id": record["extra"].get("trajectory_id", None),
        }
        ddtags = ",".join([f"{k}:{v}" for k, v in tags.items() if v is not None])

        msg = {
            "env": settings.ENV.value,
            "trajectory_id": record["extra"].get("trajectory_id", None),
            "level": record["level"].name,
            "file": record["file"].path,
            "line": record["line"],
            "function": record["function"],
            "module": record["module"],
            "process": record["process"].name,
            "thread": record["thread"].name,
            "extra": record["extra"],
            "message": record["message"],
        }

        log_item = HTTPLogItem(
            ddtags=ddtags,
            message=json.dumps(msg, default=str),
            service="rl-studio-trajectory",
        )
        _ = LogsApi(api_client=api_client).submit_log(body=HTTPLog([log_item]))
    except Exception as e:
        logger.debug(f"Error sending log to Datadog: {e}")
