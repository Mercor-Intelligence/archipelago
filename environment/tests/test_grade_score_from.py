"""The score pass of a relayed grade (/grade/relay/score) reads no filesystem; the collect pass does."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from runner import grade as grade_mod
from runner import grading_paths
from runner.grade import (
    GradeRequest,
    RelayGradeRequest,
    RelayScoreRequest,
    RelayVerdict,
    _relay_args,
)

COLLECTED: dict[str, Any] = {
    "grading_run_status": "completed",
    "verifier_results": [
        {"verifier_id": "a", "score": 0.0, "verifier_result_values": {}}
    ],
    "relay_prompts": [
        {"verifier_id": "a", "messages": [{"role": "user", "content": "q"}]}
    ],
}

_BASE: dict[str, Any] = {
    "grading_run_id": "gr_1",
    "trajectory_id": "traj_1",
    "trajectory_json": "{}",
    "grading_settings_json": "{}",
    "verifiers_json": "[]",
    "eval_configs_json": "[]",
    "scoring_config_json": "{}",
}


def _collect(**over: Any) -> RelayGradeRequest:
    return RelayGradeRequest(**{**_BASE, **over})


def _score(**over: Any) -> RelayScoreRequest:
    fields: dict[str, Any] = {
        **_BASE,
        "verdicts": {"a": RelayVerdict(content="{}")},
        "score_from": COLLECTED,
    }
    fields.update(over)
    return RelayScoreRequest(**fields)


class _Launched(Exception):
    """Raised in place of the CLI, carrying the command and the rows file it was handed."""

    def __init__(self, cmd: tuple[str, ...], score_from: dict[str, Any] | None) -> None:
        super().__init__("cli launched")
        self.cmd = cmd
        self.score_from = score_from


class _RecordCmd:
    async def __call__(self, *cmd: str, **_kw: Any) -> Any:
        # Read here: the grade dir is a TemporaryDirectory and is gone once `_grade` unwinds.
        score_from = None
        if "--score-from" in cmd:
            score_from = json.loads(
                Path(cmd[cmd.index("--score-from") + 1]).read_text()
            )
        raise _Launched(cmd, score_from)


async def _launch(
    request: GradeRequest, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> tuple[list[str], list[str], dict[str, Any] | None]:
    """Run `_grade` up to the CLI launch; return (cmd, filesystem steps run, rows file)."""
    touched: list[str] = []
    monkeypatch.setattr(grade_mod, "_GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(work_dir))
    monkeypatch.setattr(grade_mod, "grading_available", lambda: True)

    async def _materialize(_r: Any, dest: Path) -> None:
        touched.append("materialize")
        dest.write_bytes(b"")

    def _capture(dest: Path, _globs: Any = None) -> None:
        touched.append("capture")
        dest.write_bytes(b"")

    monkeypatch.setattr(grade_mod, "_materialize_baseline", _materialize)
    monkeypatch.setattr(grade_mod, "_materialize_task_half", _materialize)
    monkeypatch.setattr(grade_mod, "_capture_live_final_snapshot", _capture)
    monkeypatch.setattr(grade_mod.asyncio, "create_subprocess_exec", _RecordCmd())
    try:
        await grade_mod._grade(request)
    except _Launched as launched:
        return list(launched.cmd), touched, launched.score_from
    raise AssertionError("the CLI was never launched")


@pytest.mark.asyncio
async def test_the_score_pass_touches_no_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmd, touched, score_from = await _launch(_score(), monkeypatch, tmp_path)

    assert touched == []
    assert cmd[cmd.index("-m") + 1] == "runner.relay_scoring"
    assert "--score-from" in cmd
    for flag in ("--initial-snapshot", "--final-snapshot", "--trajectory"):
        assert flag not in cmd, flag
    # The rows go to the CLI as a file in the 0700 grade dir, exactly what the caller sent.
    assert Path(cmd[cmd.index("--score-from") + 1]).parent.parent == tmp_path
    assert score_from == COLLECTED


@pytest.mark.asyncio
async def test_the_collect_pass_still_reads_the_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmd, touched, _ = await _launch(_collect(), monkeypatch, tmp_path)

    assert "materialize" in touched
    assert "capture" in touched
    assert cmd[cmd.index("-m") + 1] == "runner.main"
    assert "--initial-snapshot" in cmd
    assert "--final-snapshot" in cmd
    assert "--score-from" not in cmd


def test_a_score_request_must_carry_the_rows() -> None:
    """There is no third shape: the route says what it is, and its model says what it needs."""
    with pytest.raises(ValidationError):
        RelayScoreRequest(**_BASE, verdicts={"a": RelayVerdict(content="{}")})
    with pytest.raises(ValidationError):
        RelayScoreRequest(**_BASE, score_from=COLLECTED)


def test_a_collect_request_has_no_verdicts_to_carry() -> None:
    """Verdicts sent to the collect route are an ignored extra key; it is still a collect pass."""
    request = RelayGradeRequest.model_validate(
        {**_BASE, "verdicts": {"a": {"content": "{}"}}}
    )
    assert not hasattr(request, "verdicts")
    assert isinstance(request, RelayGradeRequest)
    assert not isinstance(request, RelayScoreRequest)


def test_verdicts_are_written_as_plain_completions(tmp_path: Path) -> None:
    args = _relay_args(
        _score(verdicts={"a": RelayVerdict(content='{"is_criteria_true": true}')}),
        tmp_path,
    )
    assert args[0] == "--relay-verdicts"
    assert json.loads(Path(args[1]).read_text()) == {"a": '{"is_criteria_true": true}'}


def test_a_collect_pass_writes_an_empty_verdicts_map(tmp_path: Path) -> None:
    """The file's presence is what tells the CLI to relay; empty is the collect pass."""
    args = _relay_args(_collect(), tmp_path)
    assert args[0] == "--relay-verdicts"
    assert json.loads(Path(args[1]).read_text()) == {}


def test_a_plain_grade_gets_no_relay_flag(tmp_path: Path) -> None:
    assert _relay_args(GradeRequest(**_BASE), tmp_path) == []


def test_an_old_caller_sending_a_digest_is_not_rejected() -> None:
    """Extra keys on a verdict are ignored, as everywhere else on the request."""
    request = _score(verdicts={"a": {"content": "{}", "digest": "abc"}})
    assert request.verdicts["a"].content == "{}"
