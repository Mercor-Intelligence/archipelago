"""Inline goldens contract, env runner half. Every golden-reading eval depends on it.

Read `.agents/rules/inline-grading-goldens.md` before changing or deleting a test here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from runner import grade as grade_mod
from runner import grade_jobs, grading_paths
from runner.grade import GradeRequest
from runner.sweep import SweepResult


def _request(**over: Any) -> GradeRequest:
    base: dict[str, Any] = {
        "grading_run_id": "gr_1",
        "trajectory_id": "traj_1",
        "trajectory_json": "{}",
        "grading_settings_json": "{}",
        "verifiers_json": "[]",
        "eval_configs_json": "{}",
        "scoring_config_json": "{}",
        "initial_snapshot_url": "https://s3.amazonaws.com/world.zip",
    }
    base.update(over)
    return GradeRequest(**base)


class _Reached(Exception):
    """Raised in place of the CLI, once the command line is built."""


async def _run_to_cli(
    request: GradeRequest,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    *,
    goldens_allowed: bool,
) -> tuple[list[str], list[str]]:
    """Run `_grade` up to the CLI launch; return the URLs fetched and the command."""
    fetched: list[str] = []
    cmd: list[str] = []
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(work_dir))

    async def _download(url: str, dest: Path) -> None:
        fetched.append(url)
        dest.write_bytes(b"")

    def _capture(dest: Path, _globs: Any = None) -> None:
        dest.write_bytes(b"")

    async def _exec(*argv: str, **_kw: Any) -> Any:
        cmd.extend(argv)
        raise _Reached

    monkeypatch.setattr(grade_mod, "_download_snapshot", _download)
    monkeypatch.setattr(grade_mod, "_capture_live_final_snapshot", _capture)
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", _exec)
    try:
        await grade_mod._grade(request, goldens_allowed=goldens_allowed)  # noqa: SLF001
    except _Reached:
        pass
    return fetched, cmd


@pytest.mark.asyncio
async def test_an_allowed_grade_fetches_each_golden_and_names_it_to_the_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lane hands the CLI every golden with its id, and the verifier picks
    its own by id, so the pairing has to survive the trip."""
    request = _request(
        golden_snapshot_urls=[
            "https://s3.amazonaws.com/gold_a.zip",
            "https://s3.amazonaws.com/gold_b.zip",
        ],
        golden_snapshot_ids=["snap_a", "snap_b"],
    )

    fetched, cmd = await _run_to_cli(
        request, monkeypatch, tmp_path, goldens_allowed=True
    )

    assert fetched == [
        "https://s3.amazonaws.com/world.zip",
        "https://s3.amazonaws.com/gold_a.zip",
        "https://s3.amazonaws.com/gold_b.zip",
    ]
    pairs = [
        (Path(cmd[i + 1]).name, cmd[i + 3])
        for i, arg in enumerate(cmd)
        if arg == "--golden-snapshot"
    ]
    assert pairs == [("golden_0.zip", "snap_a"), ("golden_1.zip", "snap_b")]


@pytest.mark.asyncio
async def test_unpaired_golden_ids_are_refused_before_anything_is_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A golden without its id cannot be matched to the verifier that reads it."""
    request = _request(golden_snapshot_urls=["https://s3.amazonaws.com/gold.zip"])

    with pytest.raises(HTTPException) as exc:
        await _run_to_cli(request, monkeypatch, tmp_path, goldens_allowed=True)

    assert exc.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clean", "signing", "expected"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_only_a_clean_sweep_under_signing_accepts_goldens(
    monkeypatch: pytest.MonkeyPatch, clean: bool, signing: bool, expected: bool
) -> None:
    """Unsigned, anyone on localhost could sweep with a reference that kills
    nothing and come back clean."""
    monkeypatch.setattr(grade_jobs._SWEEP_STATE, "goldens_accepted", False)  # noqa: SLF001
    result = SweepResult() if clean else SweepResult(survivors=[4242])
    monkeypatch.setattr(grade_jobs, "sweep_agent_processes", lambda _ticks: result)
    monkeypatch.setattr(grade_jobs, "signing_enabled", lambda: signing)

    response = await grade_jobs.sweep(grade_jobs.SweepRequest(reference_ticks=1.0))

    assert response.accepts_goldens is expected
    assert grade_jobs._SWEEP_STATE.goldens_accepted is expected  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_grade_start_passes_the_sweeps_answer_to_the_grade(
    monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(grade_jobs._SWEEP_STATE, "goldens_accepted", accepted)  # noqa: SLF001
    monkeypatch.setattr(grade_jobs, "grading_available", lambda: True)

    def _start(_request: GradeRequest, *, goldens_allowed: bool = False) -> str:
        started.append(goldens_allowed)
        return "job_1"

    monkeypatch.setattr(grade_jobs, "start_grade_job", _start)

    await grade_jobs.grade_start(_request())

    assert started == [accepted]


@pytest.mark.asyncio
async def test_the_blocking_route_refuses_goldens_even_after_a_clean_signed_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hosted-envs posts to `/grade`, and its refusal must not move with ours."""
    monkeypatch.setattr(grade_jobs._SWEEP_STATE, "goldens_accepted", True)  # noqa: SLF001
    monkeypatch.setattr(grade_mod, "grading_available", lambda: True)
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(tmp_path))
    request = _request(
        golden_snapshot_urls=["https://s3.amazonaws.com/gold.zip"],
        golden_snapshot_ids=["snap_gold"],
    )

    with pytest.raises(HTTPException) as exc:
        await grade_mod.grade(request)

    assert exc.value.status_code == 409
    assert "bake them into the image" in exc.value.detail
