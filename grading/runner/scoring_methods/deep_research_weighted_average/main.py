"""Deep Research Weighted Average scoring method.

Simple weighted average using numerical_weight from verifier config.
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


async def deep_research_weighted_average_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """
    Calculate score using weighted average based on numerical_weight.

    Formula:
    - weighted_sum = Σ(score_i × numerical_weight_i)
    - total_weights = Σ(numerical_weight_i)
    - final_score = weighted_sum / total_weights

    Args:
        verifier_results: Results from all verifiers
        verifiers: Verifier configs (used for numerical_weight)
        scoring_config_values: Configuration (unused for now)

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

    # Calculate weighted sum
    weighted_sum = 0.0
    total_weights = 0.0
    verifier_count = 0

    for result in verifier_results:
        verifier = verifier_map.get(result.verifier_id)
        if verifier is None:
            logger.warning(f"No verifier found for result {result.verifier_id}")
            continue

        # Get numerical_weight from verifier_values, default to 1.0
        # Handle None explicitly (can happen when user clears a previously-set value)
        numerical_weight = verifier.verifier_values.get("numerical_weight")
        if numerical_weight is None:
            numerical_weight = 1.0
        else:
            numerical_weight = float(numerical_weight)

        # Handle zero weight - skip this verifier entirely
        if numerical_weight == 0:
            logger.debug(
                f"[SCORING] verifier={result.verifier_id} | skipped (weight=0)"
            )
            continue

        # Add to weighted sum (works for both positive and negative weights)
        weighted_sum += result.score * numerical_weight

        # Only positive weights contribute to denominator
        # Negative weights subtract from score but don't affect max possible
        if numerical_weight > 0:
            total_weights += numerical_weight

        verifier_count += 1

        logger.debug(
            f"[SCORING] verifier={result.verifier_id} | score={result.score} | weight={numerical_weight}"
        )

    # Calculate final score
    if total_weights > 0:
        final_score = weighted_sum / total_weights
    else:
        final_score = 0.0

    # Clamp to [0, 1]
    final_score = max(0.0, min(1.0, final_score))

    logger.info(
        f"[SCORING] Deep Research Weighted Average | "
        f"final_score={final_score:.4f} | "
        f"weighted_sum={weighted_sum:.4f} | "
        f"total_weights={total_weights:.4f} | "
        f"verifier_count={verifier_count}"
    )

    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "weighted_sum": weighted_sum,
            "total_weights": total_weights,
            "verifier_count": verifier_count,
        },
    )
