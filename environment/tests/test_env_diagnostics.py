"""Stall diagnostics and the batched Datadog log sink."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import httpx
import pytest
from loguru import logger
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from runner.utils import diagnostics
from runner.utils.settings import get_settings


@pytest.fixture
def dd(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("DATADOG_API_KEY", "test-key")
    get_settings.cache_clear()
    module = importlib.import_module("runner.utils.datadog_logger")
    yield module
    get_settings.cache_clear()


class _Recorder:
    def __init__(
        self, delay_s: float = 0.0, fail: bool = False, fail_times: int = 0
    ) -> None:
        self.batches: list[list[Any]] = []
        self.threads: set[str] = set()
        self.delay_s = delay_s
        self.fail = fail
        self.fail_times = fail_times
        self.release = threading.Event()
        self.release.set()

    def __call__(self, items: list[Any]) -> None:
        self.threads.add(threading.current_thread().name)
        time.sleep(self.delay_s)
        _ = self.release.wait(10)
        if self.fail or self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("datadog down")
        self.batches.append(list(items))

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(item.message) for batch in self.batches for item in batch]


@pytest.fixture
def capture() -> Iterator[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    handler = logger.add(
        lambda m: records.append(
            {
                "level": m.record["level"].name,
                "message": m.record["message"],
                **m.record["extra"],
            }
        ),
        level="DEBUG",
    )
    yield records
    logger.remove(handler)


def _with_sink(sink: Any) -> int:
    return logger.add(sink, level="DEBUG")


def test_lines_ship_in_order_in_batches_with_their_write_time(dd: ModuleType) -> None:
    recorder = _Recorder()
    sink = dd.DatadogBatchSink(recorder)
    sink.start()
    handler = _with_sink(sink)
    try:
        for i in range(1200):
            logger.info("line {}", i)
    finally:
        logger.remove(handler)
        sink.close()

    messages = recorder.messages()
    assert [m["message"] for m in messages] == [f"line {i}" for i in range(1200)]
    assert all(len(batch) <= dd._BATCH_MAX_ITEMS for batch in recorder.batches)
    assert len(recorder.batches) < 1200 / 10
    assert all(m["date"] for m in messages)
    assert messages[0]["date"] <= messages[-1]["date"]


def test_a_logging_call_never_waits_for_datadog(dd: ModuleType) -> None:
    recorder = _Recorder(delay_s=2.0)
    sink = dd.DatadogBatchSink(recorder)
    sink.start()
    handler = _with_sink(sink)
    try:
        started = time.perf_counter()
        for i in range(600):
            logger.info("line {}", i)
        elapsed = time.perf_counter() - started
        senders_while_logging = set(recorder.threads)
    finally:
        logger.remove(handler)
        sink.close()

    assert elapsed < 1.0
    assert threading.current_thread().name not in senders_while_logging
    assert len(recorder.messages()) == 600


def test_the_last_lines_ship_within_seconds_with_no_further_logging(
    dd: ModuleType,
) -> None:
    recorder = _Recorder()
    sink = dd.DatadogBatchSink(recorder)
    sink.start()
    handler = _with_sink(sink)
    try:
        logger.warning("last words before a stall")
        deadline = time.monotonic() + 3
        while not recorder.batches and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [m["message"] for m in recorder.messages()] == [
            "last words before a stall"
        ]
    finally:
        logger.remove(handler)
        sink.close()


def test_a_failing_datadog_neither_raises_nor_logs_through_loguru(
    dd: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _Recorder(fail=True)
    sink = dd.DatadogBatchSink(recorder)
    sink.start()
    handler = _with_sink(sink)
    try:
        logger.info("one")
    finally:
        logger.remove(handler)
        sink.close()

    assert sink.pending() == 0
    assert "Error sending 1 logs to Datadog, dropped" in capsys.readouterr().err


def test_the_queue_is_bounded_and_drops_the_oldest(
    dd: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dd, "_BUFFER_MAX_ITEMS", 5)
    monkeypatch.setattr(dd, "_FLUSH_INTERVAL_S", 60.0)
    monkeypatch.setattr(dd, "_BATCH_MAX_ITEMS", 1000)
    recorder = _Recorder()
    sink = dd.DatadogBatchSink(recorder)
    sink.start()
    handler = _with_sink(sink)
    try:
        for i in range(8):
            logger.info("line {}", i)
        assert sink.pending() == 5
    finally:
        logger.remove(handler)
        sink.close()

    assert [m["message"] for m in recorder.messages()] == [
        f"line {i}" for i in range(3, 8)
    ]
    assert "Dropped 3 logs" in capsys.readouterr().err


def test_rpc_name_reads_the_tool_or_method() -> None:
    tool_call = {"method": "tools/call", "params": {"name": "excel_v2_list_files"}}
    assert diagnostics._rpc_name(json.dumps(tool_call).encode()) == (
        "excel_v2_list_files"
    )
    assert diagnostics._rpc_name(b'{"method": "tools/list"}') == "tools/list"
    assert diagnostics._rpc_name(b"not json") is None
    assert diagnostics._rpc_name(b"[1, 2]") is None


async def _slow(request: Request) -> JSONResponse:
    _ = await request.body()
    await asyncio.sleep(float(request.query_params.get("s", "0")))
    return JSONResponse({"ok": True})


def _app() -> diagnostics.RequestDiagnosticsMiddleware:
    return diagnostics.RequestDiagnosticsMiddleware(
        Starlette(
            routes=[
                Route("/mcp/", _slow, methods=["GET", "POST"]),
                Route("/data/snapshot", _slow, methods=["POST"]),
            ]
        )
    )


async def test_an_open_tool_call_is_visible_with_its_age_and_tool(
    monkeypatch: pytest.MonkeyPatch, capture: list[dict[str, Any]]
) -> None:
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)
    monkeypatch.setattr(get_settings(), "DIAGNOSTICS_SLOW_REQUEST_S", 0.2)
    body = {"method": "tools/call", "params": {"name": "slack_read_thread"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://env"
    ) as client:
        call = asyncio.create_task(client.post("/mcp/?s=0.5", json=body))
        await asyncio.sleep(0.3)
        during = tracker.snapshot()
        response = await call

    assert response.status_code == 200
    assert during["open_requests"] == 1
    assert during["oldest_open_mcp_request_tool"] == "slack_read_thread"
    assert during["oldest_open_request_s"] >= 0.25
    after = tracker.snapshot()
    assert after["open_requests"] == 0
    assert after["slow_requests"] == 1
    assert after["completed_requests"] == 1
    slow = [r for r in capture if r.get("env_diag_slow_request")]
    assert slow and slow[0]["tool"] == "slack_read_thread"
    assert slow[0]["http_status"] == 200
    assert slow[0]["env_diag_abandoned"] is False
    assert after["abandoned_requests"] == 0


async def test_a_tool_call_the_agent_cancelled_counts_as_a_stall(
    monkeypatch: pytest.MonkeyPatch, capture: list[dict[str, Any]]
) -> None:
    # The agent's tool_call_timeout fires before the request-age threshold, so the
    # cancelled call must be flagged on close or the stall is never recorded.
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)
    monkeypatch.setattr(get_settings(), "DIAGNOSTICS_SLOW_REQUEST_S", 0.2)
    monkeypatch.setattr(diagnostics, "STALL_ABANDONED_REQUEST_S", 0.25)
    body = {"method": "tools/call", "params": {"name": "slack_read_thread"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://env"
    ) as client:
        call = asyncio.create_task(client.post("/mcp/?s=5", json=body))
        await asyncio.sleep(0.3)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    after = tracker.snapshot()
    assert after["open_requests"] == 0
    assert after["abandoned_requests"] == 1
    assert after["slow_requests"] == 1
    assert diagnostics.DiagnosticsSampler.stall_suspected(
        {
            "loop_lag_max_s": 0.0,
            "loop_heartbeat_age_s": 0.4,
            "sampler_drift_s": 0.0,
            "oldest_open_mcp_request_s": None,
            **after,
        }
    )
    slow = [r for r in capture if r.get("env_diag_slow_request")]
    assert slow and slow[0]["env_diag_abandoned"] is True
    assert slow[0]["http_status"] is None
    assert slow[0]["tool"] == "slack_read_thread"


async def test_a_cancel_inside_the_tool_budget_is_slow_but_not_a_stall(
    monkeypatch: pytest.MonkeyPatch, capture: list[dict[str, Any]]
) -> None:
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)
    monkeypatch.setattr(get_settings(), "DIAGNOSTICS_SLOW_REQUEST_S", 0.2)
    body = {"method": "tools/call", "params": {"name": "slack_read_thread"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://env"
    ) as client:
        call = asyncio.create_task(client.post("/mcp/?s=5", json=body))
        await asyncio.sleep(0.3)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    after = tracker.snapshot()
    assert after["slow_requests"] == 1
    assert after["abandoned_requests"] == 0
    slow = [r for r in capture if r.get("env_diag_slow_request")]
    assert slow and slow[0]["env_diag_abandoned"] is True


async def _raising_app(scope: Any, receive: Any, send: Any) -> None:
    _ = await receive()
    await asyncio.sleep(0.3)
    raise RuntimeError("handler failed before any response")


async def test_a_handler_error_before_headers_is_not_an_abandonment(
    monkeypatch: pytest.MonkeyPatch, capture: list[dict[str, Any]]
) -> None:
    # The outer error handler sends the 500 after this middleware's finally ran,
    # so the status is unset here too; only a cancel counts as abandoned.
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)
    monkeypatch.setattr(get_settings(), "DIAGNOSTICS_SLOW_REQUEST_S", 0.2)
    monkeypatch.setattr(diagnostics, "STALL_ABANDONED_REQUEST_S", 0.25)
    app = diagnostics.RequestDiagnosticsMiddleware(_raising_app)
    body = {"method": "tools/call", "params": {"name": "slack_read_thread"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://env"
    ) as client:
        with pytest.raises(RuntimeError):
            await client.post("/mcp/", json=body)

    after = tracker.snapshot()
    assert after["slow_requests"] == 1
    assert after["abandoned_requests"] == 0
    slow = [r for r in capture if r.get("env_diag_slow_request")]
    assert slow and slow[0]["env_diag_abandoned"] is False
    assert slow[0]["http_status"] is None


async def test_the_long_lived_mcp_stream_get_is_not_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://env"
    ) as client:
        response = await client.get("/mcp/")

    assert response.status_code == 200
    assert tracker.snapshot()["completed_requests"] == 0


async def test_a_blocked_event_loop_is_reported_while_it_is_blocked(
    capture: list[dict[str, Any]],
) -> None:
    sampler = diagnostics.DiagnosticsSampler(
        interval_s=0.2,
        baseline_s=3600,
        tracker=diagnostics.RequestTracker(),
        pending_logs=lambda: None,
    )
    sampler.start()
    try:
        await asyncio.sleep(0.6)
        time.sleep(1.5)  # blocks the event loop, as a stalled gateway would
        stalls_during_block = [r for r in capture if r.get("env_diag_stall")]
        await asyncio.sleep(0.6)
    finally:
        await sampler.stop()

    assert stalls_during_block, "the sampler thread must report while the loop is stuck"
    assert stalls_during_block[0]["loop_heartbeat_age_s"] >= 1.0
    after = [r for r in capture if r.get("env_diag_stall")]
    assert any(r["loop_lag_max_s"] >= 1.0 for r in after)


def test_a_sample_always_carries_every_field() -> None:
    sampler = diagnostics.DiagnosticsSampler(
        interval_s=10,
        baseline_s=60,
        tracker=diagnostics.RequestTracker(),
        pending_logs=lambda: 7,
    )
    first = sampler.sample(drift_s=0.0)
    second = sampler.sample(drift_s=0.0)

    expected = {
        "sampler_drift_s",
        "loop_lag_max_s",
        "loop_heartbeat_age_s",
        "cpu_cores_used",
        "cpu_limit_cores",
        "cpu_throttled_s",
        "cpu_nr_throttled",
        "cpu_steal_pct",
        "gateway_cpu_s",
        "load1",
        "psi_cpu_some_avg10",
        "psi_memory_some_avg10",
        "psi_io_some_avg10",
        "memory_used_mb",
        "memory_limit_mb",
        "mem_total_mb",
        "mem_available_mb",
        "process_count",
        "pending_datadog_logs",
        "open_requests",
        "oldest_open_request_s",
        "oldest_open_request_path",
        "oldest_open_mcp_request_s",
        "oldest_open_mcp_request_tool",
        "slow_requests",
        "abandoned_requests",
        "completed_requests",
    }
    assert set(first) == expected
    assert set(second) == expected
    assert second["pending_datadog_logs"] == 7


@pytest.mark.parametrize(
    ("overrides", "stalled"),
    [
        ({}, False),
        ({"loop_lag_max_s": 1.0}, True),
        ({"loop_heartbeat_age_s": 1.5}, True),
        ({"sampler_drift_s": 2.0}, True),
        ({"oldest_open_mcp_request_s": 90.0}, True),
        ({"oldest_open_mcp_request_s": 89.0}, False),
        ({"oldest_open_mcp_request_s": 60.0}, False),
        ({"abandoned_requests": 1}, True),
        ({"oldest_open_request_s": 300.0}, False),
    ],
)
def test_stall_thresholds(overrides: dict[str, float], stalled: bool) -> None:
    sample: dict[str, Any] = {
        "loop_lag_max_s": 0.0,
        "loop_heartbeat_age_s": 0.4,
        "sampler_drift_s": 0.0,
        "oldest_open_mcp_request_s": None,
        **overrides,
    }
    assert diagnostics.DiagnosticsSampler.stall_suspected(sample) is stalled


def test_the_cgroup_readers_parse_real_file_shapes(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cpu.stat").write_text(
        "usage_usec 5000000\nuser_usec 4000000\nnr_throttled 12\nthrottled_usec 900000\n"
    )
    (tmp_path / "cpu.max").write_text("20000 100000\n")
    (tmp_path / "memory.current").write_text("2147483648\n")
    (tmp_path / "memory.max").write_text("max\n")
    (tmp_path / "cpu.pressure").write_text(
        "some avg10=37.50 avg60=10.00 avg300=2.00 total=1\n"
        "full avg10=1.00 avg60=0.00 avg300=0.00 total=1\n"
    )
    monkeypatch.setattr(diagnostics, "_CGROUP", tmp_path)

    assert diagnostics._kv((tmp_path / "cpu.stat").read_text())["nr_throttled"] == 12
    assert diagnostics._cpu_limit_cores() == 0.2
    assert diagnostics._memory_mb("memory.current") == 2048.0
    assert diagnostics._memory_mb("memory.max") is None
    assert diagnostics._pressure("cpu") == 37.5


def test_a_hung_datadog_request_times_out(dd: ModuleType) -> None:
    assert dd.configuration.request_timeout == 10


def test_a_transient_send_failure_is_retried_once(
    dd: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dd, "_RETRY_DELAY_S", 0.0)
    recorder = _Recorder(fail_times=1)
    sink = dd.DatadogBatchSink(recorder)
    handler = _with_sink(sink)
    try:
        logger.info("survives a reset connection")
    finally:
        logger.remove(handler)
        sink.close()

    assert [m["message"] for m in recorder.messages()] == [
        "survives a reset connection"
    ]
    assert "Error sending" not in capsys.readouterr().err


def test_lines_being_sent_still_count_as_pending(dd: ModuleType) -> None:
    recorder = _Recorder()
    recorder.release.clear()
    sink = dd.DatadogBatchSink(recorder)
    sink.start()
    handler = _with_sink(sink)
    try:
        for i in range(3):
            logger.info("line {}", i)
        deadline = time.monotonic() + 3
        while not recorder.threads and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sink.pending() == 3
        recorder.release.set()
    finally:
        logger.remove(handler)
        sink.close()

    assert sink.pending() == 0
    assert len(recorder.messages()) == 3


def test_a_restarted_sink_ships_again_without_waiting_for_close(
    dd: ModuleType,
) -> None:
    recorder = _Recorder()
    sink = dd.DatadogBatchSink(recorder)
    sink.start()
    sink.close()
    sink.start()
    handler = _with_sink(sink)
    try:
        logger.info("after restart")
        deadline = time.monotonic() + 3
        while not recorder.batches and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [m["message"] for m in recorder.messages()] == ["after restart"]
    finally:
        logger.remove(handler)
        sink.close()


async def test_a_long_snapshot_post_is_neither_slow_nor_a_stall(
    monkeypatch: pytest.MonkeyPatch, capture: list[dict[str, Any]]
) -> None:
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)
    monkeypatch.setattr(get_settings(), "DIAGNOSTICS_SLOW_REQUEST_S", 0.1)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://env"
    ) as client:
        call = asyncio.create_task(client.post("/data/snapshot?s=0.4"))
        await asyncio.sleep(0.2)
        during = tracker.snapshot()
        response = await call

    assert response.status_code == 200
    assert during["open_requests"] == 1
    assert during["oldest_open_mcp_request_s"] is None
    assert not [r for r in capture if r.get("env_diag_slow_request")]


async def test_a_hung_tool_is_named_while_an_older_snapshot_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)
    body = {"method": "tools/call", "params": {"name": "excel_v2_list_files"}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://env"
    ) as client:
        snapshot = asyncio.create_task(client.post("/data/snapshot?s=0.6"))
        await asyncio.sleep(0.1)
        tool = asyncio.create_task(client.post("/mcp/?s=0.4", json=body))
        await asyncio.sleep(0.2)
        during = tracker.snapshot()
        _ = await asyncio.gather(snapshot, tool)

    assert during["open_requests"] == 2
    assert during["oldest_open_request_path"] == "/data/snapshot"
    assert during["oldest_open_mcp_request_tool"] == "excel_v2_list_files"
    assert during["oldest_open_mcp_request_s"] < during["oldest_open_request_s"]


async def test_interval_zero_turns_request_tracking_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = diagnostics.RequestTracker()
    monkeypatch.setattr(diagnostics, "REQUEST_TRACKER", tracker)
    monkeypatch.setattr(get_settings(), "DIAGNOSTICS_INTERVAL_S", 0.0)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://env"
    ) as client:
        response = await client.post("/mcp/", json={"method": "tools/list"})

    assert response.status_code == 200
    assert tracker.snapshot()["completed_requests"] == 0
    assert diagnostics.start_diagnostics() is None
