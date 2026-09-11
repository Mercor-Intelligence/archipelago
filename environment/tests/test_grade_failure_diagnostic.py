"""What the sandbox says when the grading engine reports a failed grade.

The engine exits 0 whether the grade succeeded or failed, so the only account
of a failure is the text it wrote on the way out. These tests hold that text to
the log, which is the one sink that leaves the sandbox.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from runner import grade as grade_mod
from runner import grading_paths
from runner.grade import GradeRequest

_STDERR = b"Traceback: ValueError: Missing required field: criteria\n"


def _request(**over: Any) -> GradeRequest:
    base: dict[str, Any] = {
        "grading_run_id": "gr_diag",
        "trajectory_id": "traj_1",
        "trajectory_json": "{}",
        "grading_settings_json": "{}",
        "verifiers_json": "[]",
        "eval_configs_json": "{}",
        "scoring_config_json": "{}",
        "capture_id": "b" * 32,
    }
    base.update(over)
    return GradeRequest(**base)


class _Proc:
    """A CLI that exits cleanly and writes the result it was told to write."""

    returncode = 0

    def __init__(self, status: str) -> None:
        self._status = status
        self._out: Path | None = None

    async def __call__(self, *cmd: str, **_kw: Any) -> _Proc:
        self._out = Path(cmd[cmd.index("--output") + 1])
        return self

    async def communicate(self) -> tuple[bytes, bytes]:
        assert self._out is not None
        self._out.write_text(
            json.dumps(
                {
                    "grading_run_id": "gr_diag",
                    "grading_run_status": self._status,
                    "verifier_results": [],
                    "scoring_results": {"final_score": 0.0},
                }
            )
        )
        return b"", _STDERR


async def _run(
    status: str, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> tuple[Any, list[str]]:
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grade_mod, "grading_available", lambda: True)

    taken = grading_paths.capture_dir("b" * 32)
    taken.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(taken / "final.zip", "w"):
        pass

    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", _Proc(status))

    logged: list[str] = []
    monkeypatch.setattr(
        grade_mod.logger, "error", lambda message, *a, **k: logged.append(str(message))
    )

    response = await grade_mod._grade(_request())
    return response, logged


@pytest.mark.asyncio
async def test_a_failed_grade_logs_the_engines_own_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without this the run records only the caller's fixed string and the
    reason the grade failed exists nowhere a reader can reach."""
    response, logged = await _run("error", monkeypatch, tmp_path)

    assert response.result["grading_run_status"] == "error"
    assert any("Missing required field: criteria" in line for line in logged)
    assert any("gr_diag" in line for line in logged)


@pytest.mark.asyncio
async def test_a_completed_grade_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every healthy grade would otherwise carry a stderr dump into the log."""
    response, logged = await _run("completed", monkeypatch, tmp_path)

    assert response.result["grading_run_status"] == "completed"
    assert logged == []
