"""Every in-process LiteLLM call attempt of one rollout, via a callback so agents calling LiteLLM
directly are covered; vendor binaries are not. Nothing is recorded outside `collect_model_calls`."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

from runner.utils.decorators import llm_attempt_ctx, llm_call_id_ctx

MAX_MODEL_CALLS = 5000
# A cancelled attempt fires no end hook, so it keeps this outcome.
INCOMPLETE = "incomplete"


@dataclass
class ModelCallLog:
    """One rollout's call attempts, capped so a runaway loop cannot grow the record unbounded."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    # Bumped on every write, so a settle can tell a closed entry from a quiet log.
    changes: int = 0
    # Set when the collector exits; a task it spawned may still start calls.
    closed: bool = False
    # LiteLLM fires more than one hook for some call types (the Responses API).
    _seen: set[str] = field(default_factory=set, repr=False)
    _open: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def add(self, request_id: object, entry: dict[str, Any]) -> bool:
        """Append an attempt; False if it is a duplicate or the log is full."""
        if isinstance(request_id, str) and request_id in self._seen:
            return False
        if len(self.entries) >= MAX_MODEL_CALLS:
            self.truncated = True
            return False
        if isinstance(request_id, str):
            self._seen.add(request_id)
        self.entries.append(entry)
        self.changes += 1
        return True

    def open(self, request_id: str, entry: dict[str, Any]) -> bool:
        """Append an attempt that `close` finishes later."""
        if not self.add(request_id, entry):
            return False
        self._open[request_id] = entry
        return True

    def close(self, request_id: str, fields: dict[str, Any]) -> bool:
        """Finish an attempt `open` started; False if it did not."""
        entry = self._open.pop(request_id, None)
        if entry is None:
            return False
        entry.update(fields)
        self.changes += 1
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        """A copy, so an event landing after the rollout cannot change a result already built."""
        return [dict(entry) for entry in self.entries]


_active: ContextVar[ModelCallLog | None] = ContextVar("model_call_log", default=None)
# By LiteLLM's call id: a sync stream's end hooks run on a thread pool without the caller's context.
_by_request: dict[str, ModelCallLog] = {}


def _epoch(moment: object) -> float | None:
    return moment.timestamp() if isinstance(moment, datetime) else None


def _model(kwargs: dict[str, Any]) -> object:
    """`provider/model` whichever call type reported it: chat strips the provider, Responses
    keeps it."""
    model, provider = kwargs.get("model"), kwargs.get("custom_llm_provider")
    if (
        isinstance(model, str)
        and isinstance(provider, str)
        and not model.startswith(f"{provider}/")
    ):
        return f"{provider}/{model}"
    return model


class _ModelCallRecorder(CustomLogger):
    """Opens an entry before the request is sent, in the caller's context, and closes it from
    whichever thread or task LiteLLM reports the outcome on."""

    def log_pre_api_call(self, model, messages, kwargs):  # noqa: ANN001, ANN201 -- LiteLLM's hook signature
        log, request_id = _active.get(), kwargs.get("litellm_call_id")
        if log is None or log.closed or not isinstance(request_id, str):
            return
        entry = {
            "call_id": llm_call_id_ctx.get() or request_id,
            "attempt": llm_attempt_ctx.get(),
            "model": _model(kwargs),
            # LiteLLM's own clock, the one `end_time` comes from, so both ends convert alike.
            "started_at": _epoch(kwargs.get("api_call_start_time")) or time.time(),
            "finished_at": None,
            "outcome": INCOMPLETE,
        }
        if log.open(request_id, entry):
            _by_request[request_id] = log

    def _record(
        self, kwargs: dict[str, Any], start: object, end: object, outcome: str
    ) -> None:
        request_id = kwargs.get("litellm_call_id")
        fields = {"finished_at": _epoch(end), "outcome": outcome}
        if isinstance(request_id, str):
            opened = _by_request.pop(request_id, None)
            if opened is not None and opened.close(request_id, fields):
                return
        log = _active.get()
        if log is None or log.closed:
            return
        log.add(
            request_id,
            {
                "call_id": llm_call_id_ctx.get() or request_id,
                "attempt": llm_attempt_ctx.get(),
                "model": _model(kwargs),
                "started_at": _epoch(start),
                **fields,
            },
        )

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001, ANN201 -- LiteLLM's hook signature
        self._record(kwargs, start_time, end_time, "ok")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001, ANN201 -- LiteLLM's hook signature
        error = kwargs.get("exception")
        self._record(kwargs, start_time, end_time, f"error:{type(error).__name__}")

    def log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001, ANN201 -- LiteLLM's hook signature
        self._record(kwargs, start_time, end_time, "ok")

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001, ANN201 -- LiteLLM's hook signature
        error = kwargs.get("exception")
        self._record(kwargs, start_time, end_time, f"error:{type(error).__name__}")


_RECORDER = _ModelCallRecorder()


_FLUSH_TIMEOUT_S = 5.0
_SETTLE_S = 0.05


async def _settle(log: ModelCallLog) -> None:
    """Wait until LiteLLM's queue is drained and the log stopped changing. Its hooks are queued
    by a task scheduled after the call returns, so one flush can run before the last arrives."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _FLUSH_TIMEOUT_S
    previous = -1
    while log.changes != previous and loop.time() < deadline:
        previous = log.changes
        await asyncio.sleep(_SETTLE_S)
        with suppress(Exception):
            await asyncio.wait_for(
                GLOBAL_LOGGING_WORKER.flush(), deadline - loop.time()
            )


@asynccontextmanager
async def collect_model_calls() -> AsyncIterator[ModelCallLog]:
    """Log every LiteLLM call attempt made inside the block, by this task and its children."""
    if _RECORDER not in litellm.callbacks:
        litellm.callbacks.append(_RECORDER)
    log = ModelCallLog()
    token = _active.set(log)
    body_cancelled = False
    try:
        yield log
    except asyncio.CancelledError:
        body_cancelled = True
        raise
    finally:
        try:
            await _settle(log)
        except asyncio.CancelledError:
            # Consumed on purpose, as run_agent does: raising here would drop the finished rollout.
            # A body already cancelled keeps the count, or an enclosing timeout would eat its cancel.
            if not body_cancelled and (task := asyncio.current_task()) is not None:
                task.uncancel()
        log.closed = True
        # A copy: a sync call's end hook can close an entry from LiteLLM's thread pool.
        for request_id in list(log._open):  # noqa: SLF001 -- the log's own bookkeeping
            _by_request.pop(request_id, None)
        _active.reset(token)
