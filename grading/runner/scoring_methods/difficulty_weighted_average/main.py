"""Difficulty Weighted Average scoring method.

Reads a categorical `difficulty` field (Low / Medium / High) from each verifier's
`verifier_values` and computes a weighted average. The Low/Medium/High → points
mapping defaults to 1 / 3 / 5 but can be overridden per scoring config.

Pairs naturally with the OUTPUT_LLM_DIFFICULTY_WEIGHTED verifier, but works with
any verifier that exposes a `difficulty` field.
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

DIFFICULTY_LOW = "Low"
DIFFICULTY_MEDIUM = "Medium"
DIFFICULTY_HIGH = "High"

DEFAULT_DIFFICULTY_WEIGHTS: dict[str, float] = {
    DIFFICULTY_LOW: 1.0,
    DIFFICULTY_MEDIUM: 3.0,
    DIFFICULTY_HIGH: 5.0,
}
DEFAULT_DIFFICULTY = DIFFICULTY_MEDIUM


def _coerce_weight(raw: object, default: float) -> float:
    """Convert a raw config value to float, falling back when None/missing/invalid.

    Mirrors the pattern in `deep_research_weighted_average`: when a user clears
    a previously-set NUMBER field in the UI, the frontend sends `null`, which
    becomes Python `None`. `dict.get(key, default)` only returns `default` for
    *missing* keys, so we explicitly normalize None and invalid values here.
    """
    if raw is None:
        return default
    if isinstance(raw, (int, float, str)):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    logger.warning(f"[SCORING] Invalid weight value {raw!r}; falling back to {default}")
    return default


def _resolve_weights(scoring_config_values: dict[str, Any]) -> dict[str, float]:
    """Build a Low/Medium/High → weight map, allowing per-config overrides."""
    return {
        DIFFICULTY_LOW: _coerce_weight(
            scoring_config_values.get("low_weight"),
            DEFAULT_DIFFICULTY_WEIGHTS[DIFFICULTY_LOW],
        ),
        DIFFICULTY_MEDIUM: _coerce_weight(
            scoring_config_values.get("medium_weight"),
            DEFAULT_DIFFICULTY_WEIGHTS[DIFFICULTY_MEDIUM],
        ),
        DIFFICULTY_HIGH: _coerce_weight(
            scoring_config_values.get("high_weight"),
            DEFAULT_DIFFICULTY_WEIGHTS[DIFFICULTY_HIGH],
        ),
    }


def _canonicalize_difficulty(raw: object) -> str | None:
    """Normalize a raw value to canonical Low/Medium/High, or None if unrecognized.

    Empty / None / unknown values return None so callers can decide how to fall
    back. Recognized values are matched case-insensitively (so "low", "LOW",
    "Low" all collapse to "Low").
    """
    if raw is None or raw == "":
        return None
    raw_str = str(raw).strip().lower()
    for canonical in (DIFFICULTY_LOW, DIFFICULTY_MEDIUM, DIFFICULTY_HIGH):
        if raw_str == canonical.lower():
            return canonical
    return None


def _resolve_difficulty(verifier: Verifier | None, default_difficulty: str) -> str:
    """Read `difficulty` from a verifier, falling back to the configured default."""
    raw = (verifier.verifier_values or {}).get("difficulty") if verifier else None
    canonical = _canonicalize_difficulty(raw)
    if canonical is not None:
        return canonical
    if raw is not None and raw != "":
        logger.warning(
            f"[SCORING] Unknown difficulty '{raw}' on verifier "
            f"{verifier.verifier_id if verifier else '<unknown>'}; "
            f"falling back to default '{default_difficulty}'"
        )
    return default_difficulty


async def difficulty_weighted_average_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """Weighted average using each verifier's `difficulty` field.

    Formula:
    - weight_i = weights[difficulty_i]
    - weighted_sum = Σ(score_i × weight_i)
    - total_weights = Σ(weight_i for verifiers with weight > 0)
    - final_score = clamp(weighted_sum / total_weights, 0.0, 1.0)

    Verifiers with `difficulty` mapped to weight 0 are skipped entirely.
    Errors out if any verifier has ERROR status, matching other weighted methods.
    """

    verifier_errors = [
        vr for vr in verifier_results if vr.status == VerifierResultStatus.ERROR
    ]
    if verifier_errors:
        error_msg = format_verifier_errors(verifier_errors, verifiers)
        logger.error(error_msg)
        raise ValueError(error_msg)

    weights = _resolve_weights(scoring_config_values)
    raw_default = scoring_config_values.get("default_difficulty", DEFAULT_DIFFICULTY)
    default_difficulty = _canonicalize_difficulty(raw_default)
    if default_difficulty is None:
        logger.warning(
            f"[SCORING] Invalid default_difficulty '{raw_default}' in "
            f"scoring_config_values; falling back to '{DEFAULT_DIFFICULTY}'"
        )
        default_difficulty = DEFAULT_DIFFICULTY

    verifier_map = {v.verifier_id: v for v in verifiers}

    weighted_sum = 0.0
    total_weights = 0.0
    counts = {DIFFICULTY_LOW: 0, DIFFICULTY_MEDIUM: 0, DIFFICULTY_HIGH: 0}
    skipped_zero_weight = 0
    verifier_count = 0

    for result in verifier_results:
        verifier = verifier_map.get(result.verifier_id)
        difficulty = _resolve_difficulty(verifier, default_difficulty)
        weight = weights[difficulty]

        if weight == 0:
            skipped_zero_weight += 1
            logger.debug(
                f"[SCORING] verifier={result.verifier_id} | difficulty={difficulty} | "
                f"skipped (weight=0)"
            )
            continue

        weighted_sum += result.score * weight
        total_weights += weight
        counts[difficulty] += 1
        verifier_count += 1

        logger.debug(
            f"[SCORING] verifier={result.verifier_id} | score={result.score} | "
            f"difficulty={difficulty} | weight={weight}"
        )

    if total_weights > 0:
        final_score = weighted_sum / total_weights
    else:
        final_score = 0.0

    final_score = max(0.0, min(1.0, final_score))

    logger.info(
        f"[SCORING] Difficulty Weighted Average | "
        f"final_score={final_score:.4f} | "
        f"weighted_sum={weighted_sum:.4f} | "
        f"total_weights={total_weights:.4f} | "
        f"low={counts[DIFFICULTY_LOW]} medium={counts[DIFFICULTY_MEDIUM]} "
        f"high={counts[DIFFICULTY_HIGH]} | "
        f"skipped_zero_weight={skipped_zero_weight} | "
        f"verifier_count={verifier_count}"
    )

    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "weighted_sum": weighted_sum,
            "total_weights": total_weights,
            "verifier_count": verifier_count,
            "low_count": counts[DIFFICULTY_LOW],
            "medium_count": counts[DIFFICULTY_MEDIUM],
            "high_count": counts[DIFFICULTY_HIGH],
            "skipped_zero_weight": skipped_zero_weight,
            "low_weight": weights[DIFFICULTY_LOW],
            "medium_weight": weights[DIFFICULTY_MEDIUM],
            "high_weight": weights[DIFFICULTY_HIGH],
            "final_score_percentage": final_score * 100.0,
        },
    )
