"""Modal stdout sink for grading containers.

Modal drains stdout into Datadog, so a line carries the same fields the Datadog sink
does (``export_fields``): no event text, allowlisted extras.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import loguru

from runner.utils.logging.export_fields import (
    export_extra,
    export_message,
    exported_exception,
    exports_text,
)


def export_stdout_sink(message: loguru.Message) -> None:
    record = message.record
    exception = record["exception"]
    line: dict[str, Any] = {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "source": f"{record['name']}:{record['function']}:{record['line']}",
        "message": export_message(record),
        "extra": export_extra(record["extra"]),
    }
    if exception is not None and exception.type is not None:
        line["exception_type"] = exception.type.__name__
    if exception is not None and exports_text(record):
        line["exception"] = exported_exception(record)
    sys.stdout.write(json.dumps(line, default=str) + "\n")
    sys.stdout.flush()
