"""Fixtures for tests of the grade routes: a grade that prepares small files and never starts its
engine, and the command line it would have started it with."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from runner import grade as grade_mod
from runner import grading_paths
from runner.grade import GradeRequest


class _EngineLaunched(Exception):
    def __init__(self, cmd: tuple[str, ...]) -> None:
        super().__init__("engine launched")
        self.cmd = cmd


@pytest.fixture
def grade_work_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The work dir of grades whose baseline, task half and live capture are small files, and
    whose engine never starts."""
    work = tmp_path / "work"
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(work))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(work))

    async def materialize(_request: object, dest: Path) -> None:
        dest.write_bytes(b"baseline")

    def capture(dest: Path, _globs: object = None) -> None:
        dest.write_bytes(b"final" * 10_000)

    async def launch(*cmd: str, **_kwargs: object) -> None:
        raise _EngineLaunched(cmd)

    monkeypatch.setattr(grade_mod, "_materialize_baseline", materialize)
    monkeypatch.setattr(grade_mod, "_materialize_task_half", materialize)
    monkeypatch.setattr(grade_mod, "_capture_live_final_snapshot", capture)
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", launch)
    return work


@pytest.fixture
def launched(grade_work_dir: Path) -> Callable[[GradeRequest], Awaitable[list[str]]]:
    """The command line a grade would start the engine with."""

    async def command_line(request: GradeRequest) -> list[str]:
        with pytest.raises(_EngineLaunched) as launch:
            await grade_mod._grade(request)
        return list(launch.value.cmd)

    return command_line
