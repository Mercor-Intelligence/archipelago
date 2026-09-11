"""Task Score Unweighted + Universal Penalty scoring method.

Port of: verifier/runner/utils/scoring/calculation.py
"""

from typing import Any

from loguru import logger

from runner.models import (
    ScoringMethodResult,
    Verifier,
    VerifierResult,
    VerifierResultStatus,
)
from runner.scoring_methods.utils import format_verifier_errors


async def task_score_unweighted_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """
    Calculate score using unweighted average of task verifiers minus capped universal penalty.

    Formula:
    - task_score = sum(task_scores) / count(task_verifiers)
    - universal_penalty = sum(negative_universal_scores) / total_negative_points
    - final_score = task_score - (universal_penalty * penalty_cap)

    Args:
        verifier_results: Results from all verifiers
        verifiers: Verifier configs (used to determine task vs universal)
        scoring_config_values: Configuration (penalty_cap, total_negative_points)

    Returns:
        ScoringMethodResult with final score and metadata
    """

    # Check if any verifier failed - if so, raise an error
    verifier_errors = [
        vr for vr in verifier_results if vr.status == VerifierResultStatus.ERROR
    ]
    if verifier_errors:
        error_msg = format_verifier_errors(verifier_errors, verifiers)
        logger.error(error_msg)
        raise ValueError(error_msg)

    verifier_map = {v.verifier_id: v for v in verifiers}

    # Separate task-specific vs universal verifiers
    task_results = []
    universal_results = []

    for result in verifier_results:
        verifier = verifier_map[result.verifier_id]
        # Task verifiers have task_id set
        if verifier.task_id is not None:
            task_results.append(result)
        else:
            # Universal verifiers have no task_id (world_id set instead)
            universal_results.append(result)

    # Calculate task score (unweighted average)
    if not task_results:
        # No task verifiers - default to perfect score
        task_score = 1.0
    else:
        cumulative_score = sum(r.score for r in task_results)
        task_score = cumulative_score / len(task_results)

    # Calculate universal penalty
    penalty_cap = scoring_config_values.get("universal_penalty_cap", 0.2)
    total_negative = scoring_config_values.get("universal_total_negative_points", 100)

    # Sum negative scores (penalties are negative values)
    negative_points = sum(-r.score for r in universal_results if r.score < 0)

    # Calculate penalty as percentage of total available
    universal_penalty = negative_points / total_negative if total_negative > 0 else 0.0

    # Clamp to valid range [0, 1]
    universal_penalty = max(0.0, min(1.0, universal_penalty))

    # Cap the penalty
    capped_penalty = universal_penalty * penalty_cap

    # Final score = task score minus capped universal penalty
    final_score = task_score - capped_penalty

    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "task_score": task_score,
            "universal_penalty": universal_penalty,
            "capped_penalty": capped_penalty,
            "task_verifier_count": len(task_results),
            "universal_verifier_count": len(universal_results),
        },
    )
