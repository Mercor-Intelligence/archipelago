"""Whether a grading run compares with another run of the task, and which
pairwise verifiers do. Shared by the runner's skip rules and the agentic
verifier, so it imports nothing either side would not ship."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from runner.models import ScoringMethodResult, VerifierResult

PAIRWISE_REFERENCE_TRAJECTORY_ARG = "pairwise_reference_trajectory_id"
# The eval config field choosing what a pairwise verifier compares against.
PAIRWISE_REFERENCE_FIELD = "pairwise_reference"
REFERENCE_GOLDEN = "golden"
REFERENCE_ANOTHER_RUN = "another_run"


def compares_with_another_run(eval_config_values: dict[str, Any] | None) -> bool:
    """Whether a pairwise verifier compares with another run rather than the golden."""
    values = eval_config_values or {}
    return (
        values.get("pairwise_with_reference") is True
        and values.get(PAIRWISE_REFERENCE_FIELD) == REFERENCE_ANOTHER_RUN
    )


def pairwise_reference_run_id(grading_run_args: dict[str, Any] | None) -> str | None:
    """The run this grading run compares with, or None to compare with the golden."""
    value = (grading_run_args or {}).get(PAIRWISE_REFERENCE_TRAJECTORY_ARG)
    return value if isinstance(value, str) and value else None


def pairwise_run_score(results: Sequence[VerifierResult]) -> ScoringMethodResult:
    """A run graded against another run scores its comparison, never the world's rubric method."""
    from runner.models import ScoringMethodResult

    score = next(
        (
            r.verifier_result_values.get("pairwise_score")
            for r in results
            if "pairwise_verdict" in r.verifier_result_values
            or "pairwise_skipped" in r.verifier_result_values
        ),
        None,
    )
    final_score = score if isinstance(score, int | float) else None
    # A skip (e.g. the other run made no files) is a settled outcome, not a failure.
    skipped = any(
        r.verifier_result_values.get("pairwise_skipped") is True for r in results
    )
    values: dict[str, Any] = {
        "pairwise_run": True,
        "pairwise_score": score,
        "pairwise_skipped": skipped,
    }
    if final_score is None and not skipped:
        values["error"] = next(
            (r.message for r in results if r.status == "error" and r.message),
            "The comparison gave no verdict",
        )
    return ScoringMethodResult(
        final_score=final_score, scoring_method_result_values=values
    )
