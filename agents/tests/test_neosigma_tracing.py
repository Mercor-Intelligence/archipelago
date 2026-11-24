import asyncio
import hashlib
import json
from io import BytesIO

import httpx
import pytest
from opentelemetry.trace import StatusCode

from runner import main as runner_main
from runner.agents.models import AgentStatus, AgentTrajectoryOutput
from runner.models import AgentConfig
from runner.utils import neosigma_artifacts, neosigma_tracing

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
@pytest.mark.parametrize("failed_part", [None, 3])
async def test_large_artifact_confirmed_before_publication(monkeypatch, failed_part):
    monkeypatch.setenv("NEOSIGMA_API_KEY", "fixture")
    monkeypatch.setenv("NEOSIGMA_PROJECT", "fixture")
    monkeypatch.setattr(neosigma_artifacts, "_PART_BYTES", 4)
    reservations = {}
    stored = {}
    completed = []

    def respond(request):
        if request.url.host == "storage.example":
            assert "Authorization" not in request.headers
            file_id = request.url.path.rsplit("/", 1)[1]
            assert len(request.content) == reservations[file_id]["size_bytes"]
            stored[file_id] = request.content
            return httpx.Response(200)

        assert request.headers["Authorization"] == "Bearer fixture"
        if request.url.path == "/projects.get":
            assert request.url.params["project_id"] == "fixture"
            return httpx.Response(200, json={"project": {"workspace_id": "w"}})

        payload = json.loads(request.content)
        file_id = payload["file_id"]
        if request.url.path == "/files.create":
            assert set(completed) == reservations.keys()
            reservations[file_id] = payload
            return httpx.Response(
                200,
                json={
                    "file": {"id": file_id, "status": "pending"},
                    "upload_url": f"https://storage.example/{file_id}",
                    "upload_headers": {},
                },
            )
        assert request.url.path == "/files.complete"
        assert file_id in stored
        if failed_part == len(reservations):
            return httpx.Response(503)
        completed.append(file_id)
        return httpx.Response(200, json={"file": {"id": file_id, "status": "complete"}})

    install_mock_transport(monkeypatch, respond)
    content = bytes(range(256)) * 2
    if failed_part:
        with pytest.raises(
            neosigma_artifacts.SnapshotUploadError, match="part 3 of 128"
        ):
            await neosigma_artifacts.publish_artifact(
                BytesIO(content), "world.tar.gz", "application/gzip"
            )
        assert len(completed) == 2
        assert len(reservations) == 3
        return

    location, name = await neosigma_artifacts.publish_artifact(
        BytesIO(content), "world.tar.gz", "application/gzip"
    )
    assert name == "world.tar.gz.manifest.json"
    assert len(completed) == 129
    manifest = json.loads(stored[location.rsplit("/", 1)[1]])
    rebuilt = bytearray()
    for part in manifest["parts"]:
        data = stored[part["location"].rsplit("/", 1)[1]]
        assert len(data) == part["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == part["sha256"]
        rebuilt.extend(data)
    assert rebuilt == content


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
async def test_trace_only_reads_world_at_run_boundaries(monkeypatch):
    monkeypatch.setenv("NEOSIGMA_API_KEY", "fixture")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def init():
        neosigma.init(api_key="", project="fixture", tracer_provider=provider)
        return neosigma

    monkeypatch.setattr(neosigma_tracing, "_init", init)
    events = []
    refs = []

    async def read(_url, _token):
        events.append("workspace")
        await asyncio.sleep(0.025)
        return BytesIO(b"tar")

    async def publish(content, name, media_type):
        events.append(("published", name, media_type, content.read()))
        if name == "final-workspace.tar.gz":
            raise neosigma_artifacts.SnapshotUploadError(
                "Snapshot part 2 of 2 was not confirmed"
            )
        return f"fixture:{name}", name

    monkeypatch.setattr(neosigma_tracing, "read_workspace", read)
    monkeypatch.setattr(neosigma_tracing, "publish_artifact", publish)
    monkeypatch.setattr(
        neosigma, "artifact", lambda location, **kwargs: refs.append(location)
    )

    @neosigma_tracing.trace_model
    async def model(model, messages, tools):
        events.append("model")
        return {"content": "use a tool"}

    async def tool():
        events.append("tool")
        return {"success": True}

    async def agent(run_input):
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

    result = await run_fixture(monkeypatch, agent)
    assert result.status == AgentStatus.FAILED
    assert events[:5] == ["workspace", "model", "tool", "tool", "workspace"]
    assert len(events[5:]) == 4
    assert "fixture:starting-workspace.tar.gz" in refs
    assert "fixture:final-workspace.tar.gz" not in refs

    spans = exporter.get_finished_spans()
    (root,) = [span for span in spans if span.parent is None]
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["archipelago.status"] == "failed"
    assert root.attributes["archipelago.artifacts.final-workspace.tar.gz.error"] == (
        "Snapshot part 2 of 2 was not confirmed"
    )
    children = [span for span in spans if span.parent is not None]
    assert {span.name for span in children}.issuperset({"first", "second"})
    assert all(span.parent.span_id == root.context.span_id for span in children)


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
