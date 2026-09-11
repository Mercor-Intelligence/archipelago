"""When the answer key may travel into the sandbox being graded.

Goldens are the ANSWER KEY. The 0700 root-owned grade dir hides them from a
model with its own uid and from nobody else, so on a world where the model is
root the grade needs another reason before it fetches them.
"""

from __future__ import annotations

import zipfile
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
        "golden_snapshot_urls": ["https://s3.amazonaws.com/gold.zip"],
    }
    base.update(over)
    return GradeRequest(**base)


async def _grade(
    request: GradeRequest,
    monkeypatch: pytest.MonkeyPatch,
    work_dir: Path,
    *,
    take_capture: bool = True,
    fetched: list[str] | None = None,
) -> list[str]:
    """Run far enough to reach the golden decision, recording what it fetched.

    `fetched` is the caller's when given, so a test whose grade RAISES can still
    see whether the key was downloaded first.
    """
    fetched = [] if fetched is None else fetched

    # `/app/.grading` is the sandbox's root-owned dir and does not exist here.
    # Under uid separation an absent one fails the grade closed, well before
    # the decision these tests are about.
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(work_dir))

    # A named capture must be on disk, or the grade refuses. `take_capture`
    # False names one that is not there, which is what a reaped container
    # leaves behind.
    if request.capture_id and take_capture:
        taken = grading_paths.capture_dir(request.capture_id)
        taken.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(taken / "final.zip", "w"):
            pass

    async def _download(url: str, dest: Path) -> None:
        fetched.append(url)
        dest.write_bytes(b"")

    monkeypatch.setattr(grade_mod, "_download_snapshot", _download)
    monkeypatch.setattr(grade_mod, "grading_available", lambda: True)
    # Past the goldens the grade launches the CLI, which is not what these
    # tests are about, so the run stops at that seam.
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", _Stop())
    try:
        await grade_mod._grade(request)
    except _Reached:
        pass
    return fetched


class _Reached(Exception):
    """Raised to stop once the golden decision has been made."""


class _Stop:
    """Stands in for the CLI subprocess, and ends the run instead."""

    async def __call__(self, *_cmd: str, **_kw: Any) -> Any:
        raise _Reached


@pytest.mark.asyncio
async def test_a_live_sandbox_without_uid_separation_refuses_the_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No capture means the graded tree is read at grade time, so the model is
    live in a tree still being read and the key could still reach the score."""
    monkeypatch.delenv("CODE_EXEC_RUN_AS_USER", raising=False)

    with pytest.raises(HTTPException) as caught:
        _ = await _grade(_request(capture_id=""), monkeypatch, tmp_path)

    assert caught.value.status_code == 409
    assert "answer key" in str(caught.value.detail)


@pytest.mark.asyncio
async def test_uid_separation_alone_is_enough(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The dir hides the key from a model that is not root, which is the
    hosted-envs case and unchanged."""
    monkeypatch.setenv("CODE_EXEC_RUN_AS_USER", "model")

    fetched = await _grade(_request(capture_id=""), monkeypatch, tmp_path)

    assert fetched == ["https://s3.amazonaws.com/gold.zip"]


@pytest.mark.asyncio
async def test_a_pre_taken_capture_alone_is_enough(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The inline path: the graded tree was frozen before this request, so the
    key arrives after the answer it could change is fixed."""
    monkeypatch.delenv("CODE_EXEC_RUN_AS_USER", raising=False)

    fetched = await _grade(_request(capture_id="a" * 32), monkeypatch, tmp_path)

    assert fetched == ["https://s3.amazonaws.com/gold.zip"]


@pytest.mark.asyncio
async def test_a_run_with_no_goldens_is_never_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The refusal is about the key, not about the sandbox."""
    monkeypatch.delenv("CODE_EXEC_RUN_AS_USER", raising=False)

    fetched = await _grade(
        _request(capture_id="", golden_snapshot_urls=[]), monkeypatch, tmp_path
    )

    assert fetched == []


@pytest.mark.asyncio
async def test_a_named_capture_that_is_not_on_disk_refuses_before_fetching(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A capture id is what allows the key, so one naming nothing has frozen
    nothing. Captures live on the container's ephemeral disk and the inline
    path retries, so a retry whose capture is gone takes this branch."""
    monkeypatch.delenv("CODE_EXEC_RUN_AS_USER", raising=False)

    fetched: list[str] = []
    with pytest.raises(HTTPException) as caught:
        _ = await _grade(
            _request(capture_id="b" * 32),
            monkeypatch,
            tmp_path,
            take_capture=False,
            fetched=fetched,
        )

    assert caught.value.status_code == 409
    assert "not on disk" in str(caught.value.detail)
    # The point of the reorder: it refuses BEFORE the key lands.
    assert fetched == []
