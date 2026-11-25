import asyncio
import hashlib
import json
import tarfile
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.trace import StatusCode

from runner import main as runner_main
from runner.agents.models import AgentStatus, AgentTrajectoryOutput
from runner.models import AgentConfig
from runner.utils import file_staging, neosigma_artifacts, neosigma_tracing
from runner.utils.logging import main as runner_logging

neosigma = pytest.importorskip("neosigma")
sdk_spans = pytest.importorskip("neosigma.spans")
TracerProvider = pytest.importorskip("opentelemetry.sdk.trace").TracerProvider
SimpleSpanProcessor = pytest.importorskip(
    "opentelemetry.sdk.trace.export"
).SimpleSpanProcessor
InMemorySpanExporter = pytest.importorskip(
    "opentelemetry.sdk.trace.export.in_memory_span_exporter"
).InMemorySpanExporter


def install_mock_transport(monkeypatch, respond):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        neosigma_artifacts.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_part", [None, 3, "timeout", "manifest"])
async def test_large_artifact_confirmed_before_publication(monkeypatch, failed_part):
    monkeypatch.setattr(neosigma_artifacts, "_PART_BYTES", 4)
    stored = {}
    queued = []
    active = set()
    completed_parts = 0

    def upload_artifact(source, *, name, content_type, attach):
        assert attach is False
        queued.append(name)
        location = f"fixture:{name}"
        if name.endswith(".manifest.json"):
            assert completed_parts == 128
            assert content_type == "application/json"
        else:
            assert isinstance(source, Path) and source.is_file()
            assert content_type == "application/octet-stream"

        async def wait():
            nonlocal completed_parts
            active.add(name)
            assert len(active) <= 4
            try:
                await asyncio.sleep(0)
                if name.endswith("part-00003"):
                    if failed_part == "timeout":
                        await asyncio.Event().wait()
                    if failed_part == 3:
                        raise RuntimeError("SDK upload could not be confirmed")
                if name.endswith(".manifest.json"):
                    if failed_part == "manifest":
                        raise RuntimeError("SDK manifest could not be confirmed")
                    stored[location] = source
                else:
                    stored[location] = source.read_bytes()
                    completed_parts += 1
            finally:
                active.remove(name)

        return SimpleNamespace(location=location, wait=wait)

    sdk = SimpleNamespace(upload_artifact=upload_artifact)
    content = bytes(range(256)) * 2
    publication = neosigma_artifacts.publish_artifact(
        sdk, BytesIO(content), "world.tar.gz", "application/gzip"
    )
    if failed_part == "timeout":
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.25):
                await publication
    elif failed_part == 3:
        with pytest.raises(
            neosigma_artifacts.SnapshotUploadError, match="part 3 of 128"
        ):
            await publication
    elif failed_part == "manifest":
        with pytest.raises(
            neosigma_artifacts.SnapshotUploadError, match="manifest was not confirmed"
        ):
            await publication
        assert completed_parts == 128
    else:
        location, name = await publication
        assert name == "world.tar.gz.manifest.json"
        assert len(queued) == 129
        manifest = json.loads(stored[location])
        rebuilt = bytearray()
        for part in manifest["parts"]:
            data = stored[part["location"]]
            assert len(data) == part["size_bytes"]
            assert hashlib.sha256(data).hexdigest() == part["sha256"]
            rebuilt.extend(data)
        assert rebuilt == content
        return

    assert "fixture:world.tar.gz.manifest.json" not in stored
    if failed_part in (3, "timeout"):
        assert len(queued) == 4


@pytest.mark.asyncio
async def test_small_artifact_uploads_as_one_file():
    def upload_artifact(source, *, name, content_type, attach):
        assert name == "initial-messages.json"
        assert content_type == "application/json"
        assert attach is False
        assert source.read_bytes() == b"[]"

        async def wait():
            assert source.is_file()

        return SimpleNamespace(location="fixture:transcript", wait=wait)

    sdk = SimpleNamespace(upload_artifact=upload_artifact)
    assert await neosigma_artifacts.publish_artifact(
        sdk, BytesIO(b"[]"), "initial-messages.json", "application/json"
    ) == ("fixture:transcript", "initial-messages.json")


@pytest.mark.asyncio
async def test_workspace_read_uses_signed_read_only_route(monkeypatch):
    monkeypatch.setattr(
        neosigma_artifacts,
        "sign_env_runner_request",
        lambda method, url: {"X-Test-Signature": f"{method} {url}"},
    )

    def respond(request):
        assert request.url.path == "/data/snapshot/workspace"
        assert request.headers["Authorization"] == "Bearer fixture"
        assert request.headers["X-Test-Signature"].startswith("POST ")
        assert request.content == b""
        return httpx.Response(200, content=b"tar stream")

    install_mock_transport(monkeypatch, respond)
    with await neosigma_artifacts.read_workspace(
        "https://world.example", "fixture"
    ) as snapshot:
        assert snapshot.read() == b"tar stream"


async def run_fixture(monkeypatch, agent):
    monkeypatch.setattr(runner_main, "get_agent_impl", lambda _: agent)
    return await runner_main.main(
        trajectory_id="fixture",
        initial_messages=[{"role": "user", "content": "Write a file"}],
        mcp_gateway_url="https://sandbox.example/mcp/",
        mcp_gateway_auth_token="fixture",
        agent_config=AgentConfig(
            agent_config_id="react_toolbelt_agent",
            agent_name="Fixture",
            agent_config_values={},
        ),
        orchestrator_model="fixture",
        orchestrator_extra_args=None,
    )


@pytest.mark.asyncio
async def test_sdk_without_upload_receipts_preserves_traces(monkeypatch, request):
    monkeypatch.setenv("NEOSIGMA_API_KEY", "fixture")
    monkeypatch.delattr(neosigma, "PendingArtifact", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    neosigma.init(api_key="", project="fixture", tracer_provider=provider)
    request.addfinalizer(neosigma.shutdown)
    monkeypatch.setattr(neosigma_tracing, "_init", lambda: neosigma)

    async def unexpected_read(*args):
        pytest.fail("SDK without receipts should not start capture")

    monkeypatch.setattr(neosigma_tracing, "read_workspace", unexpected_read)

    async def agent(_input):
        return AgentTrajectoryOutput(
            messages=[], status=AgentStatus.COMPLETED, time_elapsed=0
        )

    result = await run_fixture(monkeypatch, agent)
    assert result.status == AgentStatus.COMPLETED
    (root,) = exporter.get_finished_spans()
    assert root.attributes["archipelago.artifacts.disabled.error"] == (
        "Confirmed uploads require NeoSigma SDK 0.19.0+"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [None, "upload", "download"])
async def test_trace_only_reads_world_at_run_boundaries(monkeypatch, request, stall):
    monkeypatch.setenv("NEOSIGMA_API_KEY", "fixture")
    monkeypatch.setenv("NEOSIGMA_CAPTURE_TIMEOUT_SECONDS", "0.25")
    if not hasattr(neosigma, "PendingArtifact"):
        monkeypatch.setattr(neosigma, "PendingArtifact", object(), raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def init():
        neosigma.init(api_key="", project="fixture", tracer_provider=provider)
        request.addfinalizer(neosigma.shutdown)
        return neosigma

    monkeypatch.setattr(neosigma_tracing, "_init", init)
    events = []
    refs = []
    staged = []

    async def stage(archive, **kwargs):
        events.append("staged")
        staged.append(archive)

    monkeypatch.setattr(file_staging, "upload_turn_archive", stage)

    async def read(_url, _token):
        events.append("workspace")
        if stall == "download" and events.count("workspace") == 2:
            await asyncio.Event().wait()
        await asyncio.sleep(0.025)
        return BytesIO(b"tar")

    async def publish(sdk, content, name, media_type):
        assert sdk is neosigma
        events.append(("published", name, media_type, content.read()))
        if name == "final-workspace.tar.gz":
            if stall == "upload":
                await asyncio.Event().wait()
            raise neosigma_artifacts.SnapshotUploadError(
                "Snapshot part 2 of 2 was not confirmed"
            )
        return f"fixture:{name}", name

    monkeypatch.setattr(neosigma_tracing, "read_workspace", read)
    monkeypatch.setattr(neosigma_tracing, "publish_artifact", publish)
    monkeypatch.setattr(
        neosigma,
        "artifact",
        lambda location, **kwargs: refs.append((location, kwargs["direction"])),
    )

    @neosigma_tracing.trace_model
    async def model(model, messages, tools):
        events.append("model")
        return {"content": "use a tool"}

    async def tool():
        events.append("tool")
        return {"success": True}

    async def agent(run_input):
        await file_staging.stage_message_files(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "file",
                            "file": {
                                "filename": "input.txt",
                                "file_data": "YWJj",
                            },
                        }
                    ],
                }
            ],
            mcp_gateway_url=run_input.mcp_gateway_url,
            auth_token=run_input.mcp_gateway_auth_token,
        )
        await model(
            run_input.orchestrator_model,
            run_input.initial_messages,
            [{"name": "write"}],
        )
        for name in ["first", "second"]:
            result = await asyncio.wait_for(
                neosigma_tracing.trace_tool(name, {"file": "report"}, tool), 0.01
            )
            assert result == {"success": True}
        return AgentTrajectoryOutput(
            messages=[],
            status=AgentStatus.FAILED,
            time_elapsed=0,
            final_answer="partial answer",
        )

    result = await asyncio.wait_for(run_fixture(monkeypatch, agent), 2)
    assert result.status == AgentStatus.FAILED
    assert events[:6] == ["workspace", "staged", "model", "tool", "tool", "workspace"]
    assert {publication[1] for publication in events[6:8]} == {
        "initial-messages.json",
        "trajectory-result.json",
    }
    with tarfile.open(fileobj=BytesIO(staged[0]), mode="r:gz") as archive:
        assert archive.extractfile("turn_inputs/turn_1/input.txt").read() == b"abc"
    assert ("fixture:initial-messages.json", "input") in refs
    assert ("fixture:trajectory-result.json", "output") in refs
    if stall == "download":
        assert len(events[6:]) == 2
    else:
        assert events[8] == (
            "published",
            "staged-inputs-001.tar.gz",
            "application/gzip",
            staged[0],
        )
        assert ("fixture:staged-inputs-001.tar.gz", "input") in refs
        assert ("fixture:starting-workspace.tar.gz", "input") in refs
    assert ("fixture:final-workspace.tar.gz", "output") not in refs

    spans = exporter.get_finished_spans()
    (root,) = [span for span in spans if span.parent is None]
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["archipelago.status"] == "failed"
    if stall:
        assert (
            root.attributes["archipelago.artifacts.after-run.error"] == "TimeoutError"
        )
    else:
        assert len(events[6:]) == 5
        assert root.attributes[
            "archipelago.artifacts.final-workspace.tar.gz.error"
        ] == ("Snapshot part 2 of 2 was not confirmed")
    children = [span for span in spans if span.parent is not None]
    assert {span.name for span in children}.issuperset({"first", "second"})
    assert all(span.parent.span_id == root.context.span_id for span in children)
    (model_span,) = [
        span for span in children if "archipelago.tool_schemas" in span.attributes
    ]
    assert json.loads(model_span.attributes["archipelago.tool_schemas"]) == [
        {"name": "write"}
    ]
    assert "tool_schemas" not in model_span.attributes["gen_ai.prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_override", [None, 0.1])
async def test_large_workspace_extends_only_the_default_budget(
    monkeypatch, timeout_override
):
    monkeypatch.setattr(neosigma_tracing, "_CAPTURE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(neosigma_tracing, "_WORKSPACE_BUDGET_BYTES", 8)
    uploaded = []

    async def read(*args):
        await asyncio.sleep(0.15)
        return BytesIO(b"final workspace bytes")

    async def attach(_sdk, _turn, content, name, _direction, _media_type):
        with content:
            if name == "starting-workspace.tar.gz":
                await asyncio.sleep(0.15)
            assert content.read()
            uploaded.append(name)

    monkeypatch.setattr(neosigma_tracing, "_collect_workspace", read)
    monkeypatch.setattr(neosigma_tracing, "_attach", attach)
    with ExitStack() as files:
        starting = files.enter_context(BytesIO(b"starting workspace bytes"))
        run = neosigma_tracing._publish_after_run(
            None, None, files, "gateway", None, starting, [], [], {}, timeout_override
        )
        if timeout_override is None:
            await run
            assert uploaded[2:] == [
                "starting-workspace.tar.gz",
                "final-workspace.tar.gz",
            ]
        else:
            with pytest.raises(TimeoutError):
                await run
            assert len(uploaded) == 2
    assert set(uploaded[:2]) == {"initial-messages.json", "trajectory-result.json"}


def sdk_failure(*args, **kwargs):
    raise ValueError("synthetic SDK error")


@pytest.mark.asyncio
async def test_sdk_span_setup_failure_preserves_call_results(monkeypatch):
    monkeypatch.setattr(sdk_spans, "start_chat", sdk_failure)
    monkeypatch.setattr(sdk_spans, "start_tool", sdk_failure)

    @neosigma_tracing.trace_model
    async def model(model, messages, tools):
        return "response"

    async def tool():
        raise RuntimeError("real tool error")

    token = neosigma_tracing._active.set(True)
    try:
        assert await model("fixture", [], []) == "response"
        with pytest.raises(RuntimeError, match="real tool error"):
            await neosigma_tracing.trace_tool("tool", {}, tool)
    finally:
        neosigma_tracing._active.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["initialize", "turn"])
async def test_sdk_turn_setup_failure_preserves_run_result(monkeypatch, failure):
    monkeypatch.setenv("NEOSIGMA_CONSOLE_EXPORT", "true")
    if failure == "initialize":
        monkeypatch.setattr(neosigma_tracing, "_init", sdk_failure)
    else:
        monkeypatch.setattr(neosigma_tracing, "_init", lambda: neosigma)
        monkeypatch.setattr(neosigma, "turn", sdk_failure)

    async def agent(_input):
        return AgentTrajectoryOutput(
            messages=[], status=AgentStatus.COMPLETED, time_elapsed=0
        )

    result = await run_fixture(monkeypatch, agent)
    assert result.status == AgentStatus.COMPLETED


@pytest.mark.asyncio
async def test_cli_persists_result_before_flushing_traces(monkeypatch):
    events = []
    outcome = AgentTrajectoryOutput(
        messages=[], status=AgentStatus.COMPLETED, time_elapsed=0
    )

    async def run():
        return outcome

    def persist(result):
        assert result is outcome
        events.append("persist")

    async def flush():
        events.append("flush")

    async def complete():
        events.append("complete")

    monkeypatch.setattr(neosigma_tracing, "flush_traces", flush)
    monkeypatch.setattr(runner_logging, "flush_cas_ledger", lambda: None)
    monkeypatch.setattr(runner_logging.logger, "complete", complete)
    monkeypatch.setattr(runner_logging, "_active_trajectory_count", 2)
    monkeypatch.setattr(runner_logging.settings, "REDIS_LOGGING", False)
    result = await runner_main.run_and_persist(
        trajectory_id="fixture",
        run=run,
        persist=persist,
        teardown=runner_logging.teardown_logger,
    )
    assert result is outcome
    assert events == ["persist", "flush", "complete"]
    assert runner_logging._active_trajectory_count == 1


@pytest.mark.asyncio
async def test_direct_worker_teardown_flushes_before_last_trajectory(monkeypatch):
    calls = []

    async def complete():
        calls.append("complete")

    async def flush():
        calls.append("flush")

    monkeypatch.setattr(neosigma_tracing, "flush_traces", flush)
    monkeypatch.setattr(runner_logging, "flush_cas_ledger", lambda: None)
    monkeypatch.setattr(runner_logging.logger, "complete", complete)
    monkeypatch.setattr(runner_logging, "_active_trajectory_count", 2)
    monkeypatch.setattr(runner_logging.settings, "REDIS_LOGGING", False)

    await runner_logging.teardown_logger()

    assert calls == ["flush", "complete"]
    assert runner_logging._active_trajectory_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 10,
        },
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 80, "cache_creation_tokens": 10},
        },
        {
            "input_tokens": 100,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 80, "cache_creation_tokens": 10},
        },
    ],
)
async def test_model_span_records_cache_tokens(request, usage):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    neosigma.init(api_key="", project="fixture", tracer_provider=provider)
    request.addfinalizer(neosigma.shutdown)

    @neosigma_tracing.trace_model
    async def model(model, messages, tools):
        return SimpleNamespace(usage=usage)

    token = neosigma_tracing._active.set(True)
    try:
        await model("fixture", [{"role": "user", "content": "hi"}], [])
    finally:
        neosigma_tracing._active.reset(token)
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.usage.input_tokens"] == 100
    assert span.attributes["gen_ai.usage.output_tokens"] == 5
    assert span.attributes["gen_ai.usage.cache_read_input_tokens"] == 80
    assert span.attributes["gen_ai.usage.cache_creation_input_tokens"] == 10
