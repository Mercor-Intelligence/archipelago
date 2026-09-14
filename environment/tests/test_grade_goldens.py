"""Goldens are baked into the image at build time; a grade request that carries them is refused."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from runner import grade as grade_mod
from runner import grading_paths
from runner.grade import GradeRequest


def _request(**over: Any) -> GradeRequest:
    base: dict[str, Any] = {
        "grading_run_id": "gr_1",
        "trajectory_id": "traj_1",
        "trajectory_json": "{}",
        "grading_settings_json": "{}",
        "verifiers_json": "[]",
        "eval_configs_json": "{}",
        "scoring_config_json": "{}",
    }
    base.update(over)
    return GradeRequest(**base)


class _Reached(Exception):
    """Raised in place of the CLI, to stop the run once the snapshots were assembled."""


class _Stop:
    async def __call__(self, *_cmd: str, **_kw: Any) -> Any:
        raise _Reached


async def _grade(
    request: GradeRequest, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> list[str]:
    """Run `_grade` up to the CLI launch, recording every URL it downloaded."""
    fetched: list[str] = []
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grade_mod, "grading_available", lambda: True)

    async def _download(url: str, dest: Path) -> None:
        fetched.append(url)
        dest.write_bytes(b"")

    def _capture(dest: Path, _globs: Any = None) -> None:
        dest.write_bytes(b"")

    monkeypatch.setattr(grade_mod, "_download_snapshot", _download)
    monkeypatch.setattr(grade_mod, "_capture_live_final_snapshot", _capture)
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", _Stop())
    try:
        await grade_mod._grade(request)
    except _Reached:
        pass
    return fetched


@pytest.mark.asyncio
async def test_a_request_carrying_goldens_is_refused_before_anything_is_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = _request(
        initial_snapshot_url="https://s3.amazonaws.com/world.zip",
        golden_snapshot_urls=["https://s3.amazonaws.com/gold.zip"],
    )
    with pytest.raises(HTTPException) as exc:
        await _grade(request, monkeypatch, tmp_path)
    assert exc.value.status_code == 409
    assert "bake them into the image" in exc.value.detail


@pytest.mark.asyncio
async def test_the_refusal_fetches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not even the baseline: the request is wrong as a whole, so no root download happens for it."""
    fetched: list[str] = []
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(grade_mod, "grading_available", lambda: True)

    async def _download(url: str, dest: Path) -> None:
        fetched.append(url)

    monkeypatch.setattr(grade_mod, "_download_snapshot", _download)
    with pytest.raises(HTTPException):
        await grade_mod._grade(
            _request(
                initial_snapshot_url="https://s3.amazonaws.com/world.zip",
                golden_snapshot_urls=["https://s3.amazonaws.com/gold.zip"],
            )
        )
    assert fetched == []


@pytest.mark.asyncio
async def test_a_request_without_goldens_grades_normally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetched = await _grade(
        _request(initial_snapshot_url="https://s3.amazonaws.com/world.zip"),
        monkeypatch,
        tmp_path,
    )
    assert fetched == ["https://s3.amazonaws.com/world.zip"]
