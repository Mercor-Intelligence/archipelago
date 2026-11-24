import asyncio
import hashlib
import json
import tarfile
from collections import Counter
from io import BytesIO

import httpx
import pytest
from opentelemetry.trace import StatusCode

from runner import main as runner_main
from runner.agents.models import AgentStatus, AgentTrajectoryOutput
from runner.models import AgentConfig
from runner.utils import file_staging, neosigma_artifacts, neosigma_tracing

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
@pytest.mark.parametrize("failed_part", [None, 3, "timeout"])
async def test_large_artifact_confirmed_before_publication(monkeypatch, failed_part):
    monkeypatch.setenv("NEOSIGMA_API_KEY", "fixture")
    monkeypatch.setenv("NEOSIGMA_PROJECT", "fixture")
    monkeypatch.setattr(neosigma_artifacts, "_PART_BYTES", 4)
    monkeypatch.setattr(neosigma_artifacts, "_BACKOFF_SECONDS", 0)
    reservations = {}
    stored = {}
    completed = []
    deleted = []
    attempts = Counter()

    async def respond(request):
        if request.url.host == "storage.example":
            assert "Authorization" not in request.headers
            file_id = request.url.path.rsplit("/", 1)[1]
            assert len(request.content) == reservations[file_id]["size_bytes"]
            attempts["PUT", file_id] += 1
            if (
                reservations[file_id]["filename"].endswith("part-00001")
                and attempts["PUT", file_id] == 2
            ):
                return httpx.Response(412)
            stored[file_id] = request.content
            if (
                reservations[file_id]["filename"].endswith("part-00001")
                and attempts["PUT", file_id] == 1
            ):
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(200)

        assert request.headers["Authorization"] == "Bearer fixture"
        if request.url.path == "/projects.get":
            assert request.url.params["project_id"] == "fixture"
            return httpx.Response(200, json={"project": {"workspace_id": "w"}})

        payload = json.loads(request.content)
        file_id = payload["file_id"]
        attempts[request.url.path, file_id] += 1
        if request.url.path == "/files.create":
            if payload["filename"].endswith("manifest.json"):
                assert len(completed) == 128
            reservations[file_id] = payload
            if (
                payload["filename"].endswith("part-00001")
                and attempts[request.url.path, file_id] == 1
            ):
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "file": {"id": file_id, "status": "pending"},
                    "upload_url": f"https://storage.example/{file_id}",
                    "upload_headers": {},
                },
            )
        if request.url.path == "/files.delete":
            deleted.append(file_id)
            stored.pop(file_id, None)
            return httpx.Response(200)
        assert request.url.path == "/files.complete"
        assert file_id in stored
        filename = reservations[file_id]["filename"]
        if failed_part is not None and filename.endswith("part-00003"):
            if failed_part == "timeout":
                await asyncio.Event().wait()
            return httpx.Response(503)
        if filename.endswith("part-00001") and attempts[request.url.path, file_id] == 1:
            return httpx.Response(429)
        completed.append(file_id)
        return httpx.Response(200, json={"file": {"id": file_id, "status": "complete"}})

    install_mock_transport(monkeypatch, respond)
    content = bytes(range(256)) * 2
    if failed_part:
        if failed_part == "timeout":
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.25):
                    await neosigma_artifacts.publish_artifact(
                        BytesIO(content), "world.tar.gz", "application/gzip"
                    )
        else:
            with pytest.raises(
                neosigma_artifacts.SnapshotUploadError, match="part 3 of 128"
            ):
                await neosigma_artifacts.publish_artifact(
                    BytesIO(content), "world.tar.gz", "application/gzip"
                )
        assert len(completed) == 3
        assert set(deleted) == reservations.keys()
        assert not stored
        failed = next(
            key
            for key, value in reservations.items()
            if value["filename"].endswith("part-00003")
        )
        assert attempts["/files.complete", failed] == (
            1 if failed_part == "timeout" else 3
        )
        return

    location, name = await neosigma_artifacts.publish_artifact(
        BytesIO(content), "world.tar.gz", "application/gzip"
    )
    assert name == "world.tar.gz.manifest.json"
    assert len(completed) == 129
    first = next(
        key
        for key, value in reservations.items()
        if value["filename"].endswith("part-00001")
    )
    assert [
        attempts[phase, first] for phase in ("/files.create", "PUT", "/files.complete")
    ] == [2, 2, 2]
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
@pytest.mark.parametrize("stall", [False, True])
async def test_trace_only_reads_world_at_run_boundaries(monkeypatch, request, stall):
    monkeypatch.setenv("NEOSIGMA_API_KEY", "fixture")
    monkeypatch.setenv("NEOSIGMA_CAPTURE_TIMEOUT_SECONDS", "0.25")
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
        await asyncio.sleep(0.025)
        return BytesIO(b"tar")

    async def publish(content, name, media_type):
        events.append(("published", name, media_type, content.read()))
        if name == "final-workspace.tar.gz":
            if stall:
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
    assert events[6] == (
        "published",
        "staged-inputs-001.tar.gz",
        "application/gzip",
        staged[0],
    )
    with tarfile.open(fileobj=BytesIO(staged[0]), mode="r:gz") as archive:
        assert archive.extractfile("turn_inputs/turn_1/input.txt").read() == b"abc"
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

    async def teardown():
        events.append("teardown")

    monkeypatch.setattr(runner_main, "flush_traces", flush)
    result = await runner_main.run_and_persist(
        trajectory_id="fixture", run=run, persist=persist, teardown=teardown
    )
    assert result is outcome
    assert events == ["persist", "flush", "teardown"]
