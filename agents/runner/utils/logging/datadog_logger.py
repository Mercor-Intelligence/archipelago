from __future__ import annotations

import json
import os
from typing import Any

import loguru
from datadog_api_client import Configuration, ThreadedApiClient
from datadog_api_client.v2.api.logs_api import LogsApi
from datadog_api_client.v2.model.http_log import HTTPLog
from datadog_api_client.v2.model.http_log_item import HTTPLogItem
from loguru import logger

from runner.utils.logging.export_fields import export_extra, export_message
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
_modal_task_id = os.environ.get("MODAL_TASK_ID")


def datadog_payload(record: loguru.Record) -> dict[str, Any]:
    """The Datadog body for one record: allowlisted extras, and no text from an event."""
    extra = record["extra"]
    msg: dict[str, Any] = {
        "env": settings.ENV.value,
        "trajectory_id": extra.get("trajectory_id"),
        "level": record["level"].name,
        "file": record["file"].path,
        "line": record["line"],
        "function": record["function"],
        "module": record["module"],
        "process": record["process"].name,
        "thread": record["thread"].name,
        "extra": export_extra(extra),
        "message": export_message(record),
        "message_len": len(record["message"]),
    }
    if record["exception"] is not None and record["exception"].type is not None:
        msg["exception_type"] = record["exception"].type.__name__
    if _modal_task_id:
        msg["modal_task_id"] = _modal_task_id
    return msg


def datadog_sink(message: loguru.Message):
    """Send logs to Datadog."""
    record = message.record

    try:
        # Low-cardinality, so a native tag: groupable in monitors with no facet.
        tags = {
            "env": settings.ENV.value,
            "agent_config_id": record["extra"].get("agent_config_id", None),
            "trajectory_id": record["extra"].get("trajectory_id", None),
        }
        ddtags = ",".join([f"{k}:{v}" for k, v in tags.items() if v is not None])

        log_item = HTTPLogItem(
            ddtags=ddtags,
            message=json.dumps(datadog_payload(record), default=str),
            service="rl-studio-trajectory",
        )
        _ = LogsApi(api_client=api_client).submit_log(body=HTTPLog([log_item]))
    except Exception as e:
        logger.debug(f"Error sending log to Datadog: {e}")
