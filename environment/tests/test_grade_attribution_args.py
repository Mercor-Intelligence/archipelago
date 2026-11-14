"""Spend attribution travels to the grading CLI as arguments.

Absent, the judge's calls are billed to nobody: the lane reads its campaign off the
trajectory's own metadata, and a grade running in an episode sandbox has no trajectory in
Studio to read one from.
"""

from __future__ import annotations

from typing import Any

from runner.grade import _RUN_ID_PATTERN, GradeRequest, _attribution_args


def _request(**kw: Any) -> GradeRequest:
    base: dict[str, Any] = {
        "grading_run_id": "ic-1",
        "trajectory_id": "ic-1",
        "trajectory_json": "{}",
        "grading_settings_json": "{}",
        "verifiers_json": "[]",
        "eval_configs_json": "[]",
        "scoring_config_json": "{}",
    }
    return GradeRequest(**{**base, **kw})


def test_the_campaign_becomes_a_flag() -> None:
    args = _attribution_args(_request(campaign_id="camp_1"))

    assert "--campaign-id" in args
    assert args[args.index("--campaign-id") + 1] == "camp_1"


def test_an_absent_campaign_passes_no_flag() -> None:
    """An older Studio sends none, and the CLI must not be handed an empty value."""
    args = _attribution_args(_request())

    assert "--campaign-id" not in args


def test_the_established_attribution_still_travels() -> None:
    """The campaign is added beside these, not instead of them."""
    args = _attribution_args(
        _request(
            account_id="acct_1",
            studio_actor_user_id="user_1",
            trajectory_batch_id="batch_1",
            campaign_id="camp_1",
            task_id="task_1",
            world_id="world_1",
            pipeline_run_id="prun_1",
            triggered_by="synth",
            spend_purpose="delivery",
        )
    )

    for flag, value in (
        ("--account-id", "acct_1"),
        ("--actor-user-id", "user_1"),
        ("--trajectory-batch-id", "batch_1"),
        ("--campaign-id", "camp_1"),
        ("--task-id", "task_1"),
        ("--world-id", "world_1"),
        ("--pipeline-run-id", "prun_1"),
        ("--triggered-by", "synth"),
        ("--spend-purpose", "delivery"),
    ):
        assert args[args.index(flag) + 1] == value


def test_the_launch_tree_root_is_never_a_flag() -> None:
    """It travels as ROOT_PIPELINE_RUN_ID in the CLI's env: a world pinned to an older
    grading image would reject an unknown flag, but ignores an unknown env var."""
    args = _attribution_args(
        _request(pipeline_run_id="prun_1", root_pipeline_run_id="prun_root")
    )

    assert "prun_root" not in args


def test_only_a_run_id_shaped_root_reaches_the_cli_env() -> None:
    assert _RUN_ID_PATTERN.fullmatch("prun_21f327dae7a643df95ef5b1befef8e32")
    for value in ("", "prun_", "LD_PRELOAD=/x", "prun_a b", "prun_" + "a" * 65):
        assert not _RUN_ID_PATTERN.fullmatch(value)
