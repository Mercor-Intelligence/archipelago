"""``golden_score``: the method's score without the criteria the config exempts.

A failed re-score records only ``golden_score_error``, never the exempt ids.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from runner.models import ScoringMethodResult, Verifier, VerifierResult
from runner.scoring_methods.utils import resolve_verifier_weight

GOLDEN_EXEMPT_CUSTOM_FIELD_ID = "golden_exempt_custom_field_id"
GOLDEN_SCORE = "golden_score"
GOLDEN_EXEMPT_VERIFIER_IDS = "golden_exempt_verifier_ids"
GOLDEN_SCORE_ERROR = "golden_score_error"

_MAX_ERROR_CHARS = 500


def is_golden_exempt(verifier: Verifier, field_id: str) -> bool:
    """Whether the verifier's custom field exempts it: True or "true" only."""
    raw = (verifier.verifier_custom_field_values or {}).get(field_id)
    return raw is True or (isinstance(raw, str) and raw.lower() == "true")


def _has_positive_weight(
    results: list[VerifierResult], verifiers: list[Verifier], config: dict[str, Any]
) -> bool:
    by_id = {v.verifier_id: v for v in verifiers}
    weight_field = config.get("weight_custom_field_id") or None
    weight_value = config.get("weight_verifier_value_id") or None
    return any(
        (verifier := by_id.get(r.verifier_id)) is not None
        and resolve_verifier_weight(verifier, weight_field, weight_value) > 0
        for r in results
    )


async def golden_score_values(
    impl: Callable[..., Awaitable[ScoringMethodResult]],
    results: list[VerifierResult],
    verifiers: list[Verifier],
    config: dict[str, Any],
    scored: ScoringMethodResult,
    *,
    reporting_verifiers: list[Verifier],
) -> dict[str, Any]:
    """The golden keys to merge into ``scored``'s result values; {} when unset."""
    field_id = config.get(GOLDEN_EXEMPT_CUSTOM_FIELD_ID)
    if not field_id or not isinstance(field_id, str):
        return {}
    exempt = {
        v.verifier_id for v in reporting_verifiers if is_golden_exempt(v, field_id)
    }
    if not any(v.verifier_id in exempt for v in verifiers):
        return {
            GOLDEN_SCORE: scored.final_score,
            GOLDEN_EXEMPT_VERIFIER_IDS: sorted(exempt),
        }
    kept_results = [r for r in results if r.verifier_id not in exempt]
    kept_verifiers = [v for v in verifiers if v.verifier_id not in exempt]
    # An empty rubric scores 0.0, which would make a golden unreachable.
    if not _has_positive_weight(kept_results, kept_verifiers, config):
        return {
            GOLDEN_SCORE_ERROR: "no positive-weight criterion remains "
            "without the exempt criteria"
        }
    try:
        golden = await impl(kept_results, kept_verifiers, config)
    except Exception as exc:  # noqa: BLE001 - the solver score must still land
        logger.warning(f"[SCORING] golden score failed without exempt criteria: {exc}")
        return {GOLDEN_SCORE_ERROR: str(exc)[:_MAX_ERROR_CHARS]}
    if golden.final_score is None:
        return {
            GOLDEN_SCORE_ERROR: "the scoring method returned no score "
            "without the exempt criteria"
        }
    logger.info(
        f"[SCORING] golden_score={golden.final_score:.4f} | "
        f"exempt={len(exempt)} | final_score={scored.final_score}"
    )
    return {
        GOLDEN_SCORE: golden.final_score,
        GOLDEN_EXEMPT_VERIFIER_IDS: sorted(exempt),
    }
