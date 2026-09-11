"""Task Score Weighted Sum + Universal Penalty scoring method.

Port of: verifier/runner/utils/scoring/calculation.py (weighted method)
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


async def task_score_weighted_sum_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """
    Calculate score using weighted sum based on primary/non-primary/negative objectives.

    Formula:
    - cumulative = (primary × primary_factor) + (non-primary × non_primary_factor) + (negative × negative_factor)
    - max_possible = count(primary) × primary_factor + count(non-primary) × non_primary_factor
    - task_score = cumulative / max_possible
    - final_score = task_score - (universal_penalty × cap)

    Args:
        verifier_results: Results from all verifiers
        verifiers: Verifier configs (used for task_id, is_primary_objective)
        scoring_config_values: Configuration (scaling factors, penalty_cap)

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

    # Build lookup map
    verifier_map = {v.verifier_id: v for v in verifiers}

    # Separate task vs universal
    task_results = []
    universal_results = []

    for result in verifier_results:
        verifier = verifier_map[result.verifier_id]
        if verifier.task_id is not None:
            task_results.append(result)
        else:
            universal_results.append(result)

    # Get scaling factors
    primary_factor = scoring_config_values.get(
        "task_primary_objective_scaling_factor", 4.0
    )
    non_primary_factor = scoring_config_values.get(
        "task_non_primary_objective_scaling_factor", 2.0
    )
    negative_factor = scoring_config_values.get("task_negative_scaling_factor", 0.2)

    # Calculate weighted cumulative score
    cumulative_score = 0.0
    max_possible_score = 0.0

    for result in task_results:
        verifier = verifier_map[result.verifier_id]
        is_primary = verifier.verifier_values.get("is_primary_objective", True)

        if result.score >= 0:
            # Positive objective
            factor = primary_factor if is_primary else non_primary_factor
            cumulative_score += result.score * factor
            max_possible_score += factor
        else:
            # Negative objective (penalty)
            cumulative_score += result.score * negative_factor

    # Normalize to 0-1 scale
    if max_possible_score > 0:
        task_score = cumulative_score / max_possible_score
    else:
        # No positive objectives
        task_score = 0.0

    # Calculate universal penalty (same as unweighted)
    penalty_cap = scoring_config_values.get("universal_penalty_cap", 0.2)
    total_negative = scoring_config_values.get("universal_total_negative_points", 100)

    negative_points = sum(-r.score for r in universal_results if r.score < 0)
    universal_penalty = negative_points / total_negative if total_negative > 0 else 0.0

    # Clamp to valid range [0, 1]
    universal_penalty = max(0.0, min(1.0, universal_penalty))

    capped_penalty = universal_penalty * penalty_cap

    # Final score
    final_score = task_score - capped_penalty

    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "task_score": task_score,
            "universal_penalty": universal_penalty,
            "capped_penalty": capped_penalty,
            "cumulative_score": cumulative_score,
            "max_possible_score": max_possible_score,
            "task_verifier_count": len(task_results),
            "universal_verifier_count": len(universal_results),
        },
    )
