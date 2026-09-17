"""Criteria-Level Clamped Score method.

This method clamps negative scores at the criteria level before calculating the final score.
Negative scores (from violated negative_criteria) are converted to 0 and saved to the database,
ensuring all criteria scores are in the [0, 1] range before averaging.
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


async def criteria_level_clamped_average_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """
    Calculate score by clamping negative criteria scores to 0, then averaging.

    This method transforms negative scores (typically -1.0 from violated negative_criteria)
    to 0.0 before calculating the unweighted average. This ensures that negative criteria
    violations are treated as complete failures (0) rather than penalties below zero.

    IMPORTANT: This method MODIFIES verifier_results in-place. Negative scores are
    converted to 0.0 and saved to the database.

    Formula:
    - For each verifier result:
      - If score < 0: set score = 0.0 (modified in-place)
      - If score >= 0: use as-is
    - final_score = sum(scores) / count(verifiers)

    Args:
        verifier_results: Results from all verifiers (MODIFIED in-place)
        verifiers: Verifier configs (not used in this simple method)
        scoring_config_values: Configuration (not used in this simple method)

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

    if not verifier_results:
        logger.warning("No verifier results to score, returning 0.0")
        return ScoringMethodResult(
            final_score=0.0,
            scoring_method_result_values={
                "total_count": 0,
                "positive_count": 0,
                "zero_count": 0,
                "negative_count": 0,
                "original_average": 0.0,
                "final_score_percentage": 0.0,
            },
        )

    # Modify scores in-place and track statistics
    positive_count = 0  # score > 0
    zero_count = 0  # score = 0
    negative_count = 0  # score < 0 (before clamping)
    original_sum = 0.0  # sum before clamping

    for result in verifier_results:
        original_sum += result.score
        if result.score < 0:
            negative_count += 1
            logger.debug(
                f"Clamping negative score for {result.verifier_id}: {result.score} -> 0.0"
            )
            result.score = 0.0  # Modify in-place
            zero_count += 1
        elif result.score == 0:
            zero_count += 1
        else:  # score > 0
            positive_count += 1

    # Calculate averages and percentage
    total_count = len(verifier_results)
    original_average = original_sum / total_count
    final_score = sum(r.score for r in verifier_results) / total_count
    final_score_percentage = final_score * 100.0  # Convert to percentage

    logger.info(
        f"Criteria-level clamped scoring: {negative_count} negative scores clamped, "
        f"final_score={final_score:.3f} (original_average={original_average:.3f}), "
        f"positive={positive_count}, zero={zero_count}, negative={negative_count}"
    )

    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "total_count": total_count,
            "positive_count": positive_count,
            "zero_count": zero_count,
            "negative_count": negative_count,
            "original_average": original_average,
            "final_score_percentage": final_score_percentage,
        },
    )
