"""Datadog logging sink for the environment."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable

import loguru
from datadog_api_client import ApiClient, Configuration
from datadog_api_client.v2.api.logs_api import LogsApi
from datadog_api_client.v2.model.http_log import HTTPLog
from datadog_api_client.v2.model.http_log_item import HTTPLogItem

from .settings import get_settings

settings = get_settings()

if not settings.DATADOG_API_KEY:
    raise ValueError("DATADOG_API_KEY must be set to use the Datadog logger")

# Bounded so a hung intake request cannot stall the flush thread, and with it all shipping.
configuration = Configuration(request_timeout=10)
configuration.api_key["apiKeyAuth"] = settings.DATADOG_API_KEY

# Synchronous client: only the sink's own flush thread calls it.
api_client = ApiClient(configuration)

ENVIRONMENT_ID = (
    os.environ.get("MODAL_SANDBOX_ID") or f"environment_{uuid.uuid4().hex[:12]}"
)

# Bound fields are also promoted to the top level so a query reads `@vca_id`
# here and on the other Studio services, not `@extra.vca_id` on this one alone.
# These names are Datadog's or already ours, so a bound key may not take them.
_PROTECTED_KEYS = frozenset(
    {
        "host",
        "source",
        "status",
        "service",
        "trace_id",
        "date",
        "timestamp",
        "env",
        "environment_id",
        "level",
        "file",
        "line",
        "function",
        "module",
        "process",
        "thread",
        "extra",
        "message",
    }
)


# Datadog's intake takes at most 1000 entries and 5 MB per request.
_BATCH_MAX_ITEMS = 500
_BATCH_MAX_BYTES = 4_000_000
_FLUSH_INTERVAL_S = 1.0
# Bounds memory while Datadog is unreachable; the oldest lines are dropped first.
_BUFFER_MAX_ITEMS = 50_000
_RETRY_DELAY_S = 0.5


def _log_item(record: loguru.Record) -> HTTPLogItem:
    tags = {
        "env": settings.ENV.value,
        "environment_id": ENVIRONMENT_ID,
    }
    ddtags = ",".join([f"{k}:{v}" for k, v in tags.items() if v is not None])

    extra = record["extra"]
    msg = {
        "env": settings.ENV.value,
        "environment_id": ENVIRONMENT_ID,
        # When the line was written, not when it reached Datadog.
        "date": record["time"].isoformat(),
        "level": record["level"].name,
        "file": record["file"].path,
        "line": record["line"],
        "function": record["function"],
        "module": record["module"],
        "process": record["process"].name,
        "thread": record["thread"].name,
        "extra": extra,
        "message": record["message"],
        **{k: v for k, v in extra.items() if k not in _PROTECTED_KEYS},
    }
    return HTTPLogItem(
        ddtags=ddtags,
        message=json.dumps(msg, default=str),
        service="rl-studio-environment",
    )


def _submit(items: list[HTTPLogItem]) -> None:
    _ = LogsApi(api_client=api_client).submit_log(body=HTTPLog(items))


class DatadogBatchSink:
    """Loguru sink that queues records; a daemon thread ships them in batches.

    Sends at least every second, so the last lines before a stall still leave.
    """

    def __init__(self, submit: Callable[[list[HTTPLogItem]], None]) -> None:
        self._submit = submit
        self._items: deque[HTTPLogItem] = deque(maxlen=_BUFFER_MAX_ITEMS)
        self._dropped = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start (or restart after ``close``) the flush thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run, name="datadog-log-flush", daemon=True
        )
        self._thread.start()

    def write(self, message: loguru.Message) -> None:
        try:
            item = _log_item(message.record)
        except Exception as e:
            print(f"Error formatting log for Datadog: {e!r}", file=sys.stderr)
            return
        with self._lock:
            if len(self._items) == _BUFFER_MAX_ITEMS:
                self._dropped += 1
            self._items.append(item)
            full = len(self._items) >= _BATCH_MAX_ITEMS
        if full:
            self._wake.set()

    def pending(self) -> int:
        """Lines queued or being sent, not yet delivered."""
        with self._lock:
            return len(self._items) + self._in_flight

    def send_pending(self) -> None:
        with self._send_lock:
            with self._lock:
                items = list(self._items)
                self._items.clear()
                self._in_flight = len(items)
                dropped, self._dropped = self._dropped, 0
            if dropped:
                print(
                    f"Dropped {dropped} logs while the Datadog queue was full",
                    file=sys.stderr,
                )
            start = 0
            while start < len(items):
                end, size = start, 0
                while end < len(items) and end - start < _BATCH_MAX_ITEMS:
                    size += len(items[end].message)  # ASCII: json.dumps escapes
                    if end > start and size > _BATCH_MAX_BYTES:
                        break
                    end += 1
                batch, start = items[start:end], end
                self._send(batch)
                with self._lock:
                    self._in_flight -= len(batch)

    def _send(self, batch: list[HTTPLogItem]) -> None:
        """Send one batch, retrying once so a dropped keep-alive does not lose it."""
        for attempt in range(2):
            try:
                self._submit(batch)
                return
            except Exception as e:
                if attempt == 0:
                    time.sleep(_RETRY_DELAY_S)
                    continue
                # Not through loguru: that would feed the failure back into this sink.
                print(
                    f"Error sending {len(batch)} logs to Datadog, dropped: {e!r}",
                    file=sys.stderr,
                )

    def close(self) -> None:
        """Stop the flush thread and send everything still queued.

        Not named ``stop``: loguru calls that on ``logger.remove()``.
        """
        self._stopped.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=_FLUSH_INTERVAL_S * 5)
        self.send_pending()
        if self._thread is not None:
            # A thread that woke just before the stop may still hold one last send.
            self._thread.join(timeout=_FLUSH_INTERVAL_S * 25)

    def _run(self) -> None:
        while not self._stopped.is_set():
            _ = self._wake.wait(_FLUSH_INTERVAL_S)
            self._wake.clear()
            self.send_pending()


datadog_sink = DatadogBatchSink(_submit)
