"""The grading CLI's exact command lines, frozen: the relay routes and /grade/relay/inputs share them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from runner.grade import (
    GradeRequest,
    RelayGradeRequest,
    RelayScoreRequest,
    RelayVerdict,
)

_BASE: dict[str, Any] = {
    "grading_run_id": "gr_1",
    "trajectory_id": "traj_1",
    "trajectory_json": "{}",
    "grading_settings_json": "{}",
    "verifiers_json": "[]",
    "eval_configs_json": "[]",
    "scoring_config_json": "{}",
    "campaign_id": "camp_1",
    "account_id": "acct_1",
}


@pytest.fixture
def launch(launched: Any) -> Any:
    """The engine's command line for a request, its grade dir written ``{d}``."""

    async def run(request: GradeRequest) -> list[str]:
        cmd = await launched(request)
        grade_dir = str(Path(cmd[cmd.index("--output") + 1]).parent)
        return [arg.replace(grade_dir, "{d}") for arg in cmd[1:]]

    return run


@pytest.mark.asyncio
async def test_the_collect_line(launch: Any) -> None:
    assert await launch(RelayGradeRequest(**_BASE, subtraction_resolved=True)) == [
        "-m",
        "runner.main",
        "--grading-run-id",
        "gr_1",
        "--trajectory-id",
        "traj_1",
        "--initial-snapshot",
        "{d}/initial.zip",
        "--final-snapshot",
        "{d}/final.zip",
        "--trajectory",
        "{d}/trajectory.json",
        "--task-snapshot",
        "{d}/task.zip",
        "--subtraction-resolved",
        "--grading-settings",
        "{d}/grading_settings.json",
        "--verifiers",
        "{d}/verifiers.json",
        "--eval-configs",
        "{d}/eval_configs.json",
        "--scoring-config",
        "{d}/scoring_config.json",
        "--output",
        "{d}/result.json",
        "--relay-verdicts",
        "{d}/relay_verdicts.json",
        "--account-id",
        "acct_1",
        "--campaign-id",
        "camp_1",
    ]


@pytest.mark.asyncio
async def test_the_score_line(launch: Any) -> None:
    request = RelayScoreRequest(
        **_BASE, verdicts={"a": RelayVerdict(content="{}")}, score_from={"rows": []}
    )
    assert await launch(request) == [
        "-m",
        "runner.relay_scoring",
        "--grading-run-id",
        "gr_1",
        "--trajectory-id",
        "traj_1",
        "--score-from",
        "{d}/score_from.json",
        "--grading-settings",
        "{d}/grading_settings.json",
        "--verifiers",
        "{d}/verifiers.json",
        "--eval-configs",
        "{d}/eval_configs.json",
        "--scoring-config",
        "{d}/scoring_config.json",
        "--output",
        "{d}/result.json",
        "--relay-verdicts",
        "{d}/relay_verdicts.json",
        "--account-id",
        "acct_1",
        "--campaign-id",
        "camp_1",
    ]


@pytest.mark.asyncio
async def test_a_plain_grade_line_has_no_relay(launch: Any) -> None:
    line = await launch(GradeRequest(**_BASE))
    assert line[:2] == ["-m", "runner.main"]
    assert "--relay-verdicts" not in line
