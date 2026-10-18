from __future__ import annotations

import json
from typing import Any

import loguru

# Bounds what one grade adds to the result its caller polls for.
_MAX_OUTPUT_LOG_CHARS = 4_000_000


class GradingLogsCollector:
    """Loguru sink that keeps grading_logs records for the CLI's output.

    Each record has the shape the lane's API sink posts, minus the ids, which
    the caller that persists them assigns.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._chars: int = 0
        self._truncated: bool = False

    def __call__(self, message: loguru.Message) -> None:
        if self._truncated:
            return
        record = message.record
        extra = record["extra"]
        verifier_id = extra.get("verifier_id")
        if isinstance(verifier_id, str) and not verifier_id.strip():
            verifier_id = None
        log_extra = json.dumps(extra, default=str)
        self._chars += len(record["message"]) + len(log_extra)
        if self._chars > _MAX_OUTPUT_LOG_CHARS:
            self._truncated = True
            self.records.append(
                {
                    "verifier_id": None,
                    "log_timestamp": record["time"].isoformat(),
                    "log_message": (
                        f"Grading logs stop here: the rest passed the "
                        f"{_MAX_OUTPUT_LOG_CHARS} character limit"
                    ),
                    "log_level": "WARNING",
                    "log_extra": {"message_type": "grading_logs_truncated"},
                }
            )
            return
        self.records.append(
            {
                "verifier_id": verifier_id,
                "log_timestamp": record["time"].isoformat(),
                "log_message": record["message"],
                "log_level": record["level"].name,
                "log_extra": json.loads(log_extra),
            }
        )
