"""Deep Research Weighted Average scoring method.

Simple weighted average using a configurable weight from verifier config.

Optionally reads the per-verifier weight from either a named verifier value or
a world-level custom metadata field. This lets worlds reuse an existing numeric
field without rewriting verifier rows, while preserving ``numerical_weight`` as
the backwards-compatible default.
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


def _resolve_verifier_weight(
    verifier: Verifier,
    weight_custom_field_id: str | None,
    weight_verifier_value_id: str | None,
) -> float:
    """Resolve a verifier's numeric weight.

    Priority:
      1. verifier.verifier_custom_field_values[weight_custom_field_id] when
         the scoring config opts in via ``weight_custom_field_id`` and the
         field is set + parseable as float.
      2. verifier.verifier_values[weight_verifier_value_id] when configured,
         set, and parseable as float.
      3. verifier.verifier_values["numerical_weight"] when set + parseable.
      4. 1.0 (unweighted average) as the final fallback.

    Non-numeric / None / empty values fall through to the next layer rather
    than crashing the run, so a misconfigured field_id cannot wedge scoring.
    """
    if weight_custom_field_id:
        cf_values = verifier.verifier_custom_field_values or {}
        raw = cf_values.get(weight_custom_field_id)
        if raw is not None and raw != "":
            try:
                return float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    f"[SCORING] verifier={verifier.verifier_id} | "
                    f"non-numeric custom weight {raw!r} for field_id="
                    f"{weight_custom_field_id}; falling back"
                )

    verifier_value_ids = [weight_verifier_value_id or "numerical_weight"]
    if "numerical_weight" not in verifier_value_ids:
        verifier_value_ids.append("numerical_weight")

    for field_id in verifier_value_ids:
        raw = verifier.verifier_values.get(field_id)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"[SCORING] verifier={verifier.verifier_id} | "
                f"non-numeric verifier weight {raw!r} for field_id={field_id}; "
                "falling back"
            )

    return 1.0


async def deep_research_weighted_average_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """
    Calculate score using weighted average.

    Formula:
    - weighted_sum = Σ(score_i × weight_i)
    - total_weights = Σ(weight_i)
    - final_score = weighted_sum / total_weights

    Each verifier's weight is resolved by ``_resolve_verifier_weight``:
    optionally a custom_field_values entry when the scoring config sets
    ``weight_custom_field_id``, then a verifier_values entry selected by
    ``weight_verifier_value_id``, then verifier_values["numerical_weight"],
    otherwise 1.0.

    Args:
        verifier_results: Results from all verifiers
        verifiers: Verifier configs
        scoring_config_values: May include ``weight_custom_field_id`` to
            source weights from a world-level custom metadata field or
            ``weight_verifier_value_id`` to select a verifier_values field.

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

    # Optional opt-ins: resolve weight from a per-verifier custom metadata
    # field or a named verifier_values field instead of numerical_weight.
    weight_custom_field_id: str | None = (
        scoring_config_values.get("weight_custom_field_id") or None
    )
    weight_verifier_value_id: str | None = (
        scoring_config_values.get("weight_verifier_value_id") or None
    )

    # Calculate weighted sum
    weighted_sum = 0.0
    total_weights = 0.0
    verifier_count = 0
    skipped_zero_weight = 0

    for result in verifier_results:
        verifier = verifier_map.get(result.verifier_id)
        if verifier is None:
            logger.warning(f"No verifier found for result {result.verifier_id}")
            continue

        resolved_weight = _resolve_verifier_weight(
            verifier,
            weight_custom_field_id,
            weight_verifier_value_id,
        )

        # Handle zero weight - skip this verifier entirely
        if resolved_weight == 0:
            logger.debug(
                f"[SCORING] verifier={result.verifier_id} | skipped (weight=0)"
            )
            skipped_zero_weight += 1
            continue

        # Add to weighted sum (works for both positive and negative weights)
        weighted_sum += result.score * resolved_weight

        # Only positive weights contribute to denominator
        # Negative weights subtract from score but don't affect max possible
        if resolved_weight > 0:
            total_weights += resolved_weight

        verifier_count += 1

        logger.debug(
            f"[SCORING] verifier={result.verifier_id} | score={result.score} | weight={resolved_weight}"
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
        f"verifier_count={verifier_count} | "
        f"skipped_zero_weight={skipped_zero_weight}"
    )

    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "weighted_sum": weighted_sum,
            "total_weights": total_weights,
            "verifier_count": verifier_count,
            "skipped_zero_weight": skipped_zero_weight,
        },
    )
