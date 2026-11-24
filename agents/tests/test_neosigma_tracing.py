import hashlib
import json
import tarfile
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import neosigma
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, get_current_span
from pydantic import BaseModel

from runner import main as runner_main
from runner.agents.models import AgentStatus, AgentTrajectoryOutput
from runner.models import AgentConfig
from runner.utils import neosigma_artifacts, neosigma_tracing


def snapshot(files: dict[str, bytes]) -> bytes:
    data = BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for path, contents in files.items():
            member = tarfile.TarInfo(path)
            member.size = len(contents)
            member.mode = 0o750
            archive.addfile(member, BytesIO(contents))
    return data.getvalue()


def test_snapshot_delta_retains_relative_paths_empty_files_and_deletions():
    destination = BytesIO()
    current, changed, deleted = neosigma_artifacts.workspace_delta(
        BytesIO(
            snapshot(
                {
                    "filesystem/subdir/output.txt": b"written",
                    "filesystem/empty": b"",
                    ".apps_data/expert.json": b"private",
                    "filesystem/../../secrets": b"outside workspace",
                    "filesystem/tests/test.sh": b"reserved",
                    "filesystem/steps/2/solution/reference.txt": b"reserved",
                }
            )
        ),
        {
            "removed.txt": ("old digest", 0o644),
            "subdir/output.txt": (hashlib.sha256(b"written").hexdigest(), 0o644),
        },
        destination,
    )
    assert set(current) == {"subdir/output.txt", "empty"}
    assert set(changed) == set(current)
    assert deleted == ["removed.txt"]
    with ZipFile(destination) as archive:
        assert archive.read("files/subdir/output.txt") == b"written"
        assert archive.read("files/empty") == b""
        assert (
            archive.getinfo("files/subdir/output.txt").external_attr >> 16 & 0o777
            == 0o750
        )
        assert json.loads(archive.read("manifest.json")) == {
            "files": {
                path: {"sha256": digest, "mode": mode}
                for path, (digest, mode) in current.items()
            },
            "deleted": deleted,
        }


def test_large_artifact_can_be_reassembled(monkeypatch):
    uploads = []
    monkeypatch.setattr(neosigma_artifacts, "_UPLOAD_PART_BYTES", 4)
    monkeypatch.setattr(
        neosigma,
        "upload_artifact",
        lambda data, *, name, direction: (
            uploads.append((name, direction, data)) or SimpleNamespace(location=name)
        ),
    )
    parts = neosigma_artifacts.upload_parts(
        BytesIO(b"0123456789"), "state.zip", "input"
    )
    assert [name for _, name in parts] == [
        "state.zip.part-00001",
        "state.zip.part-00002",
        "state.zip.part-00003",
    ]
    assert all(direction == "input" for _, direction, _ in uploads)
    assert b"".join(data for _, _, data in uploads) == b"0123456789"


class ModelReply(BaseModel):
    response: str
    usage: dict[str, int]


@pytest.mark.asyncio
async def test_run_tracks_model_tool_and_artifact_io_on_failed_run(monkeypatch):
    monkeypatch.setenv("NEOSIGMA_API_KEY", "ns_live_fixture")
    monkeypatch.setenv("NEOSIGMA_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("NEOSIGMA_ENABLED", "true")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        neosigma_tracing,
        "_init",
        lambda: neosigma.init(
            api_key="", project="archipelago-fixture", tracer_provider=provider
        ),
    )
    workspace = {"filesystem/input.txt": b"initial"}
    requests = []
    snapshot_failed = False

    def respond(request):
        nonlocal snapshot_failed
        requests.append(request)
        if "filesystem/recovered.txt" in workspace and not snapshot_failed:
            snapshot_failed = True
            return httpx.Response(503)
        return httpx.Response(200, content=snapshot(workspace))

    client = httpx.AsyncClient
    monkeypatch.setattr(
        neosigma_artifacts.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(
        neosigma_artifacts,
        "sign_env_runner_request",
        lambda method, url, body: {"X-Test": f"{method} {url} {len(body)}"},
    )
    uploads = []
    read_refs = []

    def upload(data, *, name, direction):
        uploads.append((get_current_span().name, name, direction, data))
        return SimpleNamespace(location=f"fixture:{len(uploads)}")

    def reference(location, *, name, direction):
        read_refs.append((get_current_span().name, location, name, direction))

    monkeypatch.setattr(neosigma, "upload_artifact", upload)
    monkeypatch.setattr(neosigma, "artifact", reference)

    @neosigma_tracing.trace_model
    async def model_call(model, messages, tools):
        return ModelReply(
            response="Use a tool", usage={"prompt_tokens": 7, "completion_tokens": 2}
        )

    async def write():
        workspace["filesystem/result.txt"] = b"first"
        return {"success": True}

    async def invoke_mcp(session, tool_call):
        assert tool_call["function"]["arguments"] == '{"path":"input.txt"}'
        return await write()

    monkeypatch.setattr(neosigma_tracing, "_call_openai_tool", invoke_mcp)

    async def overwrite_and_fail():
        workspace["filesystem/result.txt"] = b"second"
        raise ValueError("tool failed")

    async def write_during_snapshot_error():
        workspace["filesystem/recovered.txt"] = b"created before recovery"
        return {}

    async def read_recovered():
        return {"success": True}

    async def run_agent(run_input):
        workspace["filesystem/turn_inputs/attachment.txt"] = b"staged"
        await neosigma_tracing.capture_staged_inputs()
        await model_call(
            run_input.orchestrator_model,
            run_input.initial_messages,
            [{"name": "write"}],
        )
        await neosigma_tracing.call_openai_tool(
            object(),
            {"function": {"name": "write", "arguments": '{"path":"input.txt"}'}},
        )
        with pytest.raises(ValueError, match="tool failed"):
            await neosigma_tracing.trace_tool(
                "overwrite", {"path": "/filesystem/result.txt"}, overwrite_and_fail
            )
        await neosigma_tracing.trace_tool(
            "write-during-outage", {}, write_during_snapshot_error
        )
        await neosigma_tracing.trace_tool(
            "read-recovered", {"path": "recovered.txt"}, read_recovered
        )
        return AgentTrajectoryOutput(
            status=AgentStatus.FAILED,
            error="failed",
            final_answer="partial answer",
            messages=[],
            time_elapsed=0,
        )

    monkeypatch.setattr(runner_main, "get_agent_impl", lambda _: run_agent)
    result = await runner_main.main(
        trajectory_id="traj-1",
        initial_messages=[{"role": "user", "content": "Write a file"}],
        mcp_gateway_url="https://sandbox.example/mcp/",
        mcp_gateway_auth_token="fixture",
        agent_config=AgentConfig(
            agent_config_id="react_toolbelt_agent",
            agent_name="Fixture",
            agent_config_values={},
        ),
        orchestrator_model="gemini-fixture",
        orchestrator_extra_args=None,
    )
    assert result.status == "failed"
    spans = exporter.get_finished_spans()
    (root,) = [span for span in spans if span.parent is None]
    assert root.attributes["archipelago.status"] == "failed"
    children = [span for span in spans if span.parent is not None]
    assert all(span.parent.span_id == root.context.span_id for span in children)
    assert any(
        span.attributes.get("gen_ai.request.model") == "gemini-fixture"
        for span in children
    )
    assert {span.name: span.status.status_code for span in children}[
        "overwrite"
    ] == StatusCode.ERROR
    assert requests and all(
        request.url.path == "/data/snapshot"
        and request.headers["Authorization"] == "Bearer fixture"
        and request.headers["X-Test"]
        for request in requests
    )
    assert {name for name, _, _, direction in read_refs if direction == "input"} == {
        "write",
        "overwrite",
        "read-recovered",
    }

    snapshots = {
        (span, name, direction): data for span, name, direction, data in uploads
    }
    assert ("read-recovered", "tool-files.zip", "output") not in snapshots
    for span_name, archive_name, direction, path, contents in [
        (root.name, "starting-files.zip", "input", "input.txt", b"initial"),
        (
            root.name,
            "staged-inputs.zip",
            "input",
            "turn_inputs/attachment.txt",
            b"staged",
        ),
        ("write", "tool-files.zip", "output", "result.txt", b"first"),
        ("overwrite", "tool-files.zip", "output", "result.txt", b"second"),
        (
            "read-recovered",
            "workspace-before-tool.zip",
            "input",
            "recovered.txt",
            b"created before recovery",
        ),
        (root.name, "final-files.zip", "output", "result.txt", b"second"),
    ]:
        with ZipFile(BytesIO(snapshots[span_name, archive_name, direction])) as archive:
            assert archive.read(f"files/{path}") == contents
