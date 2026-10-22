"""Stall diagnostics: why an agent's call into this sandbox hung.

Logs sandbox resources, event-loop lag and open requests from a daemon thread.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .logging import pending_datadog_logs
from .settings import get_settings

LOOP_BEAT_S = 0.5
STALL_LOOP_LAG_S = 1.0
STALL_SAMPLER_DRIFT_S = 2.0
STALL_OLDEST_REQUEST_S = 90.0  # past the agents' 60 s tool_call_timeout
STALL_ABANDONED_REQUEST_S = 60.0  # a cancel this late is the agent's tool_call_timeout
STALL_STACK_DUMP_S = 5.0
STACK_CHECK_S = 1.0
STACK_DUMP_MAX = 20
STACK_DUMP_FRAMES = 40
STACK_DUMP_CHARS = 8_000
_BODY_PEEK_BYTES = 16_384
_MCP_PREFIX = "/mcp"
_CGROUP = Path("/sys/fs/cgroup")


@dataclass
class _Request:
    path: str
    started: float
    body: bytearray = field(default_factory=bytearray)


def _rpc_name(body: bytes) -> str | None:
    """The tool (or JSON-RPC method) a request body names, if it can be read."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    params = payload.get("params")
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        return params["name"]
    method = payload.get("method")
    return method if isinstance(method, str) else None


class RequestTracker:
    """Open POST requests and slow completions, shared by the middleware and sampler."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[int, _Request] = {}
        self._ids = itertools.count()
        self._slow = 0
        self._abandoned = 0
        self._completed = 0

    def start(self, path: str) -> tuple[int, _Request]:
        request = _Request(path=path, started=time.monotonic())
        with self._lock:
            request_id = next(self._ids)
            self._open[request_id] = request
        return request_id, request

    def finish(
        self, request_id: int, status: int | None, *, cancelled: bool = False
    ) -> None:
        with self._lock:
            request = self._open.pop(request_id, None)
            self._completed += 1
        if request is None:
            return
        elapsed = time.monotonic() - request.started
        # Populate and snapshot POSTs are long by design; only tool calls are judged.
        if (
            request.path.startswith(_MCP_PREFIX)
            and elapsed >= get_settings().DIAGNOSTICS_SLOW_REQUEST_S
        ):
            # Cancelled before any response: the agent gave up on it. A handler that
            # raised also has no status, so cancellation is tracked separately.
            abandoned = cancelled and status is None
            with self._lock:
                self._slow += 1
                if abandoned and elapsed >= STALL_ABANDONED_REQUEST_S:
                    self._abandoned += 1
            logger.bind(
                env_diag_slow_request=True,
                env_diag_abandoned=abandoned,
                path=request.path,
                tool=_rpc_name(bytes(request.body)),
                http_status=status,  # `status` is the sink's log-level key
                duration_s=round(elapsed, 3),
            ).warning(
                f"Slow request {request.path} took {elapsed:.1f}s (status {status})"
            )

    def oldest_mcp(self) -> tuple[float | None, str | None]:
        """Age and tool of the oldest open MCP request, without resetting counters."""
        now = time.monotonic()
        with self._lock:
            oldest = min(
                (r for r in self._open.values() if r.path.startswith(_MCP_PREFIX)),
                key=lambda r: r.started,
                default=None,
            )
        if oldest is None:
            return None, None
        return round(now - oldest.started, 3), _rpc_name(bytes(oldest.body))

    def snapshot(self) -> dict[str, Any]:
        """Open-request state, and the slow/completed counts since the last snapshot."""
        now = time.monotonic()
        with self._lock:
            oldest = min(self._open.values(), key=lambda r: r.started, default=None)
            oldest_mcp = min(
                (r for r in self._open.values() if r.path.startswith(_MCP_PREFIX)),
                key=lambda r: r.started,
                default=None,
            )
            open_count = len(self._open)
            slow, self._slow = self._slow, 0
            abandoned, self._abandoned = self._abandoned, 0
            completed, self._completed = self._completed, 0
        return {
            "open_requests": open_count,
            "oldest_open_request_s": round(now - oldest.started, 3) if oldest else None,
            "oldest_open_request_path": oldest.path if oldest else None,
            "oldest_open_mcp_request_s": round(now - oldest_mcp.started, 3)
            if oldest_mcp
            else None,
            "oldest_open_mcp_request_tool": _rpc_name(bytes(oldest_mcp.body))
            if oldest_mcp
            else None,
            "slow_requests": slow,
            "abandoned_requests": abandoned,
            "completed_requests": completed,
        }


REQUEST_TRACKER = RequestTracker()


class RequestDiagnosticsMiddleware:
    """Track each POST from arrival to response end, copying (not consuming) its body.

    GETs are skipped: the MCP stream GET stays open for the whole session.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or get_settings().DIAGNOSTICS_INTERVAL_S <= 0
        ):
            await self.app(scope, receive, send)
            return
        request_id, request = REQUEST_TRACKER.start(scope.get("path", ""))
        status: int | None = None
        cancelled = False

        async def observed_receive() -> Message:
            nonlocal cancelled
            message = await receive()
            if message["type"] == "http.disconnect":
                cancelled = True
            if message["type"] == "http.request":
                room = _BODY_PEEK_BYTES - len(request.body)
                if room > 0:
                    request.body.extend(message.get("body", b"")[:room])
            return message

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            REQUEST_TRACKER.finish(request_id, status, cancelled=cancelled)


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _kv(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def _pressure(name: str) -> float | None:
    """PSI ``some avg10`` for cpu/memory/io: % of the last 10 s something was stalled."""
    text = _read(_CGROUP / f"{name}.pressure") or _read(Path(f"/proc/pressure/{name}"))
    for line in (text or "").splitlines():
        if line.startswith("some "):
            for part in line.split()[1:]:
                key, _, value = part.partition("=")
                if key == "avg10":
                    return float(value)
    return None


def _proc_stat_cpu() -> tuple[int, int] | None:
    """(total, steal) jiffies from the aggregate ``cpu`` line of /proc/stat."""
    text = _read(Path("/proc/stat"))
    if not text or not text.startswith("cpu "):
        return None
    values = [int(v) for v in text.splitlines()[0].split()[1:]]
    steal = values[7] if len(values) > 7 else 0
    return sum(values[:8]), steal


def _meminfo_mb() -> dict[str, float]:
    out: dict[str, float] = {}
    for line in (_read(Path("/proc/meminfo")) or "").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            out[key] = round(int(rest.split()[0]) / 1024, 1)
    return out


def _cpu_limit_cores() -> float | None:
    parts = (_read(_CGROUP / "cpu.max") or "").split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return round(int(parts[0]) / int(parts[1]), 2)
    return None


def _memory_mb(name: str) -> float | None:
    text = (_read(_CGROUP / name) or "").strip()
    return round(int(text) / 1_048_576, 1) if text.isdigit() else None


def _process_count() -> int | None:
    try:
        return sum(1 for entry in os.listdir("/proc") if entry.isdigit())
    except OSError:
        return None


class DiagnosticsSampler:
    """Samples sandbox resources, loop lag and open requests on a daemon thread."""

    def __init__(
        self,
        *,
        interval_s: float,
        baseline_s: float,
        tracker: RequestTracker = REQUEST_TRACKER,
        pending_logs: Callable[[], int | None] = pending_datadog_logs,
    ) -> None:
        self._interval_s = interval_s
        self._baseline_s = baseline_s
        self._tracker = tracker
        self._pending_logs = pending_logs
        self._lock = threading.Lock()
        self._loop_lag_max_s = 0.0
        self._last_beat = time.monotonic()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._beat_task: asyncio.Task[None] | None = None
        self._last_cpu_usec: int | None = None
        self._last_throttled_usec: int | None = None
        self._last_nr_throttled: int | None = None
        self._last_proc_stat: tuple[int, int] | None = None
        self._last_process_cpu = time.process_time()
        self._loop_thread_id: int | None = None
        self._stack_dumped_beat: float | None = None
        self._stack_dumps = 0

    def start(self) -> None:
        """Start the loop heartbeat on the running loop and the sampling thread."""
        self._loop_thread_id = threading.get_ident()
        self._beat_task = asyncio.get_running_loop().create_task(self._beat())
        self._thread = threading.Thread(
            target=self._run, name="env-diagnostics", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        self._stopped.set()
        if self._beat_task is not None:
            self._beat_task.cancel()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, self._interval_s * 2)

    async def _beat(self) -> None:
        while True:
            before = time.monotonic()
            await asyncio.sleep(LOOP_BEAT_S)
            now = time.monotonic()
            with self._lock:
                self._loop_lag_max_s = max(
                    self._loop_lag_max_s, now - before - LOOP_BEAT_S
                )
                self._last_beat = now

    def _run(self) -> None:
        last_tick = time.monotonic()
        last_baseline = 0.0
        tick = self._interval_s / math.ceil(self._interval_s / STACK_CHECK_S)
        while not self._stopped.wait(tick):
            try:
                self._dump_loop_stack()
            except Exception as e:
                logger.warning(f"env diagnostics stack dump failed: {e!r}")
            now = time.monotonic()
            if now - last_tick < self._interval_s - tick / 2:
                continue
            try:
                sample = self.sample(drift_s=now - last_tick - self._interval_s)
            except Exception as e:
                logger.warning(f"env diagnostics sample failed: {e!r}")
                last_tick = now
                continue
            last_tick = now
            stalled = self.stall_suspected(sample)
            if stalled or now - last_baseline >= self._baseline_s:
                last_baseline = now
                bound = logger.bind(env_diag=True, env_diag_stall=stalled, **sample)
                if stalled:
                    bound.warning(f"env diagnostics: stall suspected {sample}")
                else:
                    bound.info("env diagnostics")

    def _dump_loop_stack(self) -> None:
        """Log the blocked loop thread's stack once per stall, up to STACK_DUMP_MAX."""
        if self._loop_thread_id is None or self._stack_dumps >= STACK_DUMP_MAX:
            return
        with self._lock:
            last_beat = self._last_beat
        beat_age = time.monotonic() - last_beat
        if beat_age < STALL_STACK_DUMP_S or last_beat == self._stack_dumped_beat:
            return
        self._stack_dumped_beat = last_beat
        self._stack_dumps += 1
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return
        # Locations only: reading source lines would do file I/O and could log secrets.
        entries: list[str] = []
        while frame is not None and len(entries) < STACK_DUMP_FRAMES:
            code = frame.f_code
            entries.append(f"{code.co_filename}:{frame.f_lineno} {code.co_name}")
            frame = frame.f_back
        stack = "\n".join(reversed(entries))
        top = entries[0]
        oldest_s, oldest_tool = self._tracker.oldest_mcp()
        logger.bind(
            env_diag=True,
            env_diag_stack=True,
            loop_heartbeat_age_s=round(beat_age, 3),
            oldest_open_mcp_request_s=oldest_s,
            oldest_open_mcp_request_tool=oldest_tool,
            loop_stack_top=top,
            loop_stack=stack[-STACK_DUMP_CHARS:],
        ).warning(f"env diagnostics: event loop blocked {beat_age:.1f}s at {top}")

    @staticmethod
    def stall_suspected(sample: dict[str, Any]) -> bool:
        oldest = sample.get("oldest_open_mcp_request_s")
        return (
            sample["loop_lag_max_s"] >= STALL_LOOP_LAG_S
            or sample["loop_heartbeat_age_s"] >= STALL_LOOP_LAG_S + LOOP_BEAT_S
            or sample["sampler_drift_s"] >= STALL_SAMPLER_DRIFT_S
            or (oldest is not None and oldest >= STALL_OLDEST_REQUEST_S)
            or sample.get("abandoned_requests", 0) > 0
        )

    def sample(self, *, drift_s: float) -> dict[str, Any]:
        """One reading; CPU figures are deltas since the previous call."""
        now = time.monotonic()
        with self._lock:
            lag, self._loop_lag_max_s = self._loop_lag_max_s, 0.0
            beat_age = now - self._last_beat

        stat = _kv(_read(_CGROUP / "cpu.stat"))
        cpu_cores_used = throttled_s = nr_throttled = None
        if "usage_usec" in stat:
            if self._last_cpu_usec is not None:
                cpu_cores_used = round(
                    (stat["usage_usec"] - self._last_cpu_usec)
                    / 1e6
                    / max(self._interval_s + drift_s, 1e-3),
                    3,
                )
            self._last_cpu_usec = stat["usage_usec"]
        if "throttled_usec" in stat:
            if self._last_throttled_usec is not None:
                throttled_s = round(
                    (stat["throttled_usec"] - self._last_throttled_usec) / 1e6, 3
                )
            self._last_throttled_usec = stat["throttled_usec"]
        if "nr_throttled" in stat:
            if self._last_nr_throttled is not None:
                nr_throttled = stat["nr_throttled"] - self._last_nr_throttled
            self._last_nr_throttled = stat["nr_throttled"]

        steal_pct = None
        proc_stat = _proc_stat_cpu()
        if proc_stat is not None:
            if self._last_proc_stat is not None:
                total = proc_stat[0] - self._last_proc_stat[0]
                if total > 0:
                    steal_pct = round(
                        100 * (proc_stat[1] - self._last_proc_stat[1]) / total, 2
                    )
            self._last_proc_stat = proc_stat

        process_cpu = time.process_time()
        gateway_cpu_s = round(process_cpu - self._last_process_cpu, 3)
        self._last_process_cpu = process_cpu

        try:
            load1: float | None = round(os.getloadavg()[0], 2)
        except OSError:
            load1 = None
        meminfo = _meminfo_mb()
        return {
            "sampler_drift_s": round(drift_s, 3),
            "loop_lag_max_s": round(lag, 3),
            "loop_heartbeat_age_s": round(beat_age, 3),
            "cpu_cores_used": cpu_cores_used,
            "cpu_limit_cores": _cpu_limit_cores(),
            "cpu_throttled_s": throttled_s,
            "cpu_nr_throttled": nr_throttled,
            "cpu_steal_pct": steal_pct,
            "gateway_cpu_s": gateway_cpu_s,
            "load1": load1,
            "psi_cpu_some_avg10": _pressure("cpu"),
            "psi_memory_some_avg10": _pressure("memory"),
            "psi_io_some_avg10": _pressure("io"),
            "memory_used_mb": _memory_mb("memory.current"),
            "memory_limit_mb": _memory_mb("memory.max"),
            "mem_total_mb": meminfo.get("MemTotal"),
            "mem_available_mb": meminfo.get("MemAvailable"),
            "process_count": _process_count(),
            "pending_datadog_logs": self._pending_logs(),
            **self._tracker.snapshot(),
        }


def start_diagnostics() -> DiagnosticsSampler | None:
    """Start the sampler unless ``DIAGNOSTICS_INTERVAL_S`` is 0."""
    settings = get_settings()
    if settings.DIAGNOSTICS_INTERVAL_S <= 0:
        return None
    sampler = DiagnosticsSampler(
        interval_s=settings.DIAGNOSTICS_INTERVAL_S,
        baseline_s=settings.DIAGNOSTICS_BASELINE_S,
    )
    sampler.start()
    return sampler
