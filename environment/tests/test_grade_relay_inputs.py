"""/grade/relay/inputs streams exactly what /grade/relay would run, and the grader sidecar reads it."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from runner import grade as grade_mod
from runner import grade_inputs
from runner import grader_sidecar_server as server
from runner.grade import RelayGradeRequest
from runner.grader_sidecar_server import INPUTS_MANIFEST, INPUTS_VERSION

_BASE: dict[str, Any] = {
    "grading_run_id": "gr_1",
    "trajectory_id": "traj_1",
    "trajectory_json": '{"messages": []}',
    "grading_settings_json": "{}",
    "verifiers_json": "[]",
    "eval_configs_json": "[]",
    "scoring_config_json": "{}",
    "grading_credentials_json": json.dumps({"OPENAI_API_KEY": "the-key-value"}),
    "campaign_id": "camp_1",
}


@pytest.fixture
def work_dir(grade_work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A signing runner's grade work dir."""
    monkeypatch.setattr(grade_inputs, "signing_enabled", lambda: True)
    return grade_work_dir


async def _serve(response: Any, sends_before_leaving: int | None = None) -> bytes:
    """``response`` sent as Starlette sends it; the client leaves after that many messages."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        if sends_before_leaving is not None and len(sent) >= sends_before_leaving:
            raise OSError("client gone")
        sent.append(message)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


async def _body(request: RelayGradeRequest) -> bytes:
    return await _serve(await grade_inputs.grade_relay_inputs(request))


def _manifest(body: bytes) -> dict[str, Any]:
    tar = tarfile.open(fileobj=io.BytesIO(body), mode="r:")
    member = tar.extractfile(INPUTS_MANIFEST)
    assert member is not None
    return json.loads(member.read())


@pytest.mark.asyncio
async def test_the_manifest_comes_first_and_every_input_follows(work_dir: Path) -> None:
    body = await _body(RelayGradeRequest(**_BASE))

    names = tarfile.open(fileobj=io.BytesIO(body), mode="r:").getnames()
    assert names[0] == INPUTS_MANIFEST
    assert set(names[1:]) == {
        "eval_configs.json",
        "final.zip",
        "grading_settings.json",
        "initial.zip",
        "relay_verdicts.json",
        "scoring_config.json",
        "task.zip",
        "trajectory.json",
        "verifiers.json",
    }
    manifest = _manifest(body)
    assert set(manifest) == {"version", "args"}
    assert manifest["version"] == INPUTS_VERSION
    assert b"the-key-value" not in body, "credentials never travel in the stream"
    assert list(work_dir.iterdir()) == [], "the grade dir goes once the stream ends"


def test_the_sidecar_allows_the_credentials_this_runner_does() -> None:
    assert server.CREDENTIAL_KEYS == grade_mod._ALLOWED_GRADING_CRED_KEYS


@pytest.mark.asyncio
async def test_the_collect_line_is_the_one_grade_relay_launches(
    work_dir: Path, launched: Any
) -> None:
    request = RelayGradeRequest(**_BASE)
    cmd = await launched(request)
    grade_dir = Path(cmd[cmd.index("--output") + 1]).parent

    manifest = _manifest(await _body(request))

    assert cmd[1:3] == ["-m", server.ENGINE_MODULE]
    assert manifest["args"] == grade_inputs._portable(cmd[3:], grade_dir)
    assert all(str(work_dir) not in arg for arg in manifest["args"])


@pytest.mark.asyncio
async def test_the_sidecar_runs_exactly_the_files_the_collect_line_names(
    work_dir: Path, tmp_path: Path
) -> None:
    """The round trip: the server reads the route's own stream, and every path resolves."""
    sidecar = tmp_path / "sidecar"
    sidecar.mkdir()
    body = await _body(RelayGradeRequest(**_BASE))

    argv, output = server.command(server.unpack(io.BytesIO(body), sidecar), sidecar)

    for flag in (
        "--initial-snapshot",
        "--final-snapshot",
        "--trajectory",
        "--task-snapshot",
    ):
        assert Path(argv[argv.index(flag) + 1]).is_file(), flag
    assert (sidecar / "final.zip").read_bytes() == b"final" * 10_000
    assert output == (sidecar / "result.json").resolve()


@pytest.mark.asyncio
async def test_an_unsigned_runner_has_no_such_route(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grade_inputs, "signing_enabled", lambda: False)

    with pytest.raises(HTTPException) as absent:
        await grade_inputs.grade_relay_inputs(RelayGradeRequest(**_BASE))

    assert absent.value.status_code == 404
    assert not work_dir.exists() or list(work_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_no_engine_is_needed_here(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grade_mod, "grading_available", lambda: False)

    assert _manifest(await _body(RelayGradeRequest(**_BASE)))["args"]


@pytest.mark.asyncio
async def test_goldens_are_refused_here_too(work_dir: Path) -> None:
    request = RelayGradeRequest(
        **_BASE, golden_snapshot_urls=["https://g"], golden_snapshot_ids=["g1"]
    )
    with pytest.raises(HTTPException) as refused:
        await grade_inputs.grade_relay_inputs(request)

    assert refused.value.status_code == 409
    assert list(work_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_a_named_capture_is_refused(work_dir: Path) -> None:
    with pytest.raises(HTTPException) as refused:
        await grade_inputs.grade_relay_inputs(
            RelayGradeRequest(**_BASE, capture_id="c" * 32)
        )

    assert refused.value.status_code == 422


@pytest.mark.asyncio
async def test_a_failed_preparation_leaves_no_grade_dir(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail(_r: Any, _dest: Path) -> None:
        raise HTTPException(status_code=409, detail="no baseline")

    monkeypatch.setattr(grade_mod, "_materialize_baseline", _fail)

    with pytest.raises(HTTPException):
        await grade_inputs.grade_relay_inputs(RelayGradeRequest(**_BASE))

    assert list(work_dir.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sends", [0, 2], ids=["before-the-first-byte", "mid-stream"])
async def test_a_reader_that_leaves_still_removes_the_grade_dir(
    work_dir: Path, sends: int
) -> None:
    response = await grade_inputs.grade_relay_inputs(RelayGradeRequest(**_BASE))
    assert list(work_dir.iterdir()) != []

    with pytest.raises(ClientDisconnect):
        await _serve(response, sends_before_leaving=sends)

    assert list(work_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_preparation_waits_for_a_running_grade(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = (
        asyncio.Lock()
    )  # this test's own: a module lock binds to the first loop it waits in
    monkeypatch.setattr(grade_inputs, "_GRADE_LOCK", lock)
    async with lock:
        pending = asyncio.ensure_future(
            grade_inputs.grade_relay_inputs(RelayGradeRequest(**_BASE))
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
    response = await pending
    assert response.media_type == "application/x-tar"
    await _serve(response)
