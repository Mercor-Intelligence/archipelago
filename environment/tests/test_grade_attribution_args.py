"""Spend attribution travels to the grading CLI as arguments.

Absent, the judge's calls are billed to nobody: the lane reads its campaign off the
trajectory's own metadata, and a grade running in an episode sandbox has no trajectory in
Studio to read one from.
"""

from __future__ import annotations

from typing import Any

from runner.grade import GradeRequest, _attribution_args


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
        )
    )

    for flag, value in (
        ("--account-id", "acct_1"),
        ("--actor-user-id", "user_1"),
        ("--trajectory-batch-id", "batch_1"),
        ("--campaign-id", "camp_1"),
    ):
        assert args[args.index(flag) + 1] == value
