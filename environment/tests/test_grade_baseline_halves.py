"""The diff baseline reaches the grading CLI as its two halves.

The lane downloads the world seed and the authored `tasks/` overlay and merges
them before grading. Here they arrive as two presigned archives and the CLI
merges them, because `runner.utils.file_subtraction` owns the marker rule and
its arming answer already has three copies that must agree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from runner import grade as grade_mod
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


class _Launch:
    """Stands in for the CLI subprocess, keeping the argv the grade built."""

    def __init__(self) -> None:
        self.cmd: list[str] = []

    async def __call__(self, *cmd: str, **_: Any) -> Any:
        self.cmd = list(cmd)
        raise RuntimeError("stop here; the argv is what this test is about")


def test_the_task_overlay_defaults_to_absent() -> None:
    """A world with no authored layer must send nothing, so the CLI keeps the
    single-archive baseline it had."""
    request = _request()

    assert request.task_snapshot_url == ""
    assert request.subtraction_resolved is False


@pytest.mark.asyncio
async def test_both_halves_are_downloaded_and_passed_to_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending only the seed reads every task file as agent-created, which is a
    wrong score. The flag travels with it because only the server can answer it."""
    fetched: list[str] = []
    launch = _Launch()

    async def _download(url: str, dest: Path) -> None:
        fetched.append(url)
        dest.write_bytes(b"")

    monkeypatch.setattr(grade_mod, "_download_snapshot", _download)
    monkeypatch.setattr(
        grade_mod, "_capture_live_final_snapshot", lambda _dest, _globs=None: None
    )
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", launch)

    with pytest.raises(RuntimeError, match="stop here"):
        _ = await grade_mod._grade(  # noqa: SLF001
            _request(
                initial_snapshot_url="https://s3/world.zip",
                task_snapshot_url="https://s3/task.zip",
                subtraction_resolved=True,
            )
        )

    assert fetched == ["https://s3/world.zip", "https://s3/task.zip"]
    assert "--task-snapshot" in launch.cmd
    assert "--subtraction-resolved" in launch.cmd


@pytest.mark.asyncio
async def test_no_overlay_sends_neither_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A world-only baseline must build the argv it built before this existed."""
    launch = _Launch()

    async def _download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"")

    monkeypatch.setattr(grade_mod, "_download_snapshot", _download)
    monkeypatch.setattr(
        grade_mod, "_capture_live_final_snapshot", lambda _dest, _globs=None: None
    )
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", launch)

    with pytest.raises(RuntimeError, match="stop here"):
        _ = await grade_mod._grade(  # noqa: SLF001
            _request(initial_snapshot_url="https://s3/world.zip")
        )

    assert "--task-snapshot" not in launch.cmd
    assert "--subtraction-resolved" not in launch.cmd


@pytest.mark.asyncio
async def test_the_resolved_flag_is_withheld_when_the_run_did_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing it for a non-resolving run drops from the baseline a file that
    environment still had, and the diff reports it as agent-created."""
    launch = _Launch()

    async def _download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"")

    monkeypatch.setattr(grade_mod, "_download_snapshot", _download)
    monkeypatch.setattr(
        grade_mod, "_capture_live_final_snapshot", lambda _dest, _globs=None: None
    )
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", launch)

    with pytest.raises(RuntimeError, match="stop here"):
        _ = await grade_mod._grade(  # noqa: SLF001
            _request(
                initial_snapshot_url="https://s3/world.zip",
                task_snapshot_url="https://s3/task.zip",
                subtraction_resolved=False,
            )
        )

    assert "--task-snapshot" in launch.cmd
    assert "--subtraction-resolved" not in launch.cmd
