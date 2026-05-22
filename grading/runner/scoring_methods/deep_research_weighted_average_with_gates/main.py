"""Deep Research Weighted Average scoring method with gate-based caps.

Computes the same weighted average as ``deep_research_weighted_average`` and
then applies up to two kinds of post-hoc score caps driven by verifier custom
fields:

1. **Expert Assessment floor (Gate 1).** If fewer than
   ``expert_assessment_pass_rate_threshold`` of verifiers tagged with one of
   ``expert_assessment_values`` pass (score >= ``pass_score_threshold``), the
   final score is capped at ``expert_assessment_floor_cap``.

2. **Custom Gate failures (Gates 2/3).** Any verifier whose custom field value
   matches a key in ``gate_caps`` triggers the corresponding cap if that
   verifier fails (score < ``pass_score_threshold``). When multiple gates fire,
   the lowest cap wins.

Custom field values are matched by *value* (not field id), so the same scoring
config works across eval configs that label things consistently. Defaults
target the Atlas world conventions (``"Expert Assessment"``,
``"Gate: Missing Scope"``, ``"Gate: Ethical / Safety Violation"``) but every
threshold and cap is overridable per scoring config.
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

DEFAULT_EXPERT_ASSESSMENT_VALUES = ["Expert Assessment"]
DEFAULT_EXPERT_ASSESSMENT_PASS_RATE_THRESHOLD = 0.25
DEFAULT_EXPERT_ASSESSMENT_FLOOR_CAP = 0.30
DEFAULT_PASS_SCORE_THRESHOLD = 0.5
DEFAULT_GATE_CAPS: dict[str, float] = {
    "Gate: Missing Scope": 0.50,
    "Gate: Ethical / Safety Violation": 0.40,
}


def _verifier_field_values(verifier: Verifier | None) -> list[Any]:
    """Return all values from a verifier's custom field map (any field_id)."""
    if verifier is None:
        return []
    cf = verifier.verifier_custom_field_values or {}
    return list(cf.values())


def _has_value(verifier: Verifier | None, accepted_values: set[str]) -> bool:
    return any(
        isinstance(v, str) and v in accepted_values
        for v in _verifier_field_values(verifier)
    )


def _matched_gate_caps(
    verifier: Verifier | None, gate_caps: dict[str, float]
) -> list[tuple[str, float]]:
    matches: list[tuple[str, float]] = []
    for v in _verifier_field_values(verifier):
        if isinstance(v, str) and v in gate_caps:
            matches.append((v, gate_caps[v]))
    return matches


async def deep_research_weighted_average_with_gates_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """Weighted average with Expert Assessment floor + custom Gate caps.

    Final score is the minimum of the base weighted average and any triggered
    caps, clamped to [0, 1].
    """
    verifier_errors = [
        vr for vr in verifier_results if vr.status == VerifierResultStatus.ERROR
    ]
    if verifier_errors:
        error_msg = format_verifier_errors(verifier_errors, verifiers)
        logger.error(error_msg)
        raise ValueError(error_msg)

    expert_assessment_values: set[str] = set(
        scoring_config_values.get(
            "expert_assessment_values", DEFAULT_EXPERT_ASSESSMENT_VALUES
        )
        or []
    )
    expert_assessment_pass_rate_threshold = float(
        scoring_config_values.get(
            "expert_assessment_pass_rate_threshold",
            DEFAULT_EXPERT_ASSESSMENT_PASS_RATE_THRESHOLD,
        )
    )
    expert_assessment_floor_cap = float(
        scoring_config_values.get(
            "expert_assessment_floor_cap", DEFAULT_EXPERT_ASSESSMENT_FLOOR_CAP
        )
    )
    pass_score_threshold = float(
        scoring_config_values.get("pass_score_threshold", DEFAULT_PASS_SCORE_THRESHOLD)
    )
    raw_gate_caps = scoring_config_values.get("gate_caps", DEFAULT_GATE_CAPS) or {}
    gate_caps: dict[str, float] = {k: float(v) for k, v in raw_gate_caps.items()}

    verifier_map = {v.verifier_id: v for v in verifiers}

    weighted_sum = 0.0
    total_weights = 0.0
    verifier_count = 0

    expert_total = 0
    expert_passed = 0
    triggered_gates: list[dict[str, Any]] = []

    for result in verifier_results:
        verifier = verifier_map.get(result.verifier_id)
        if verifier is None:
            logger.warning(f"No verifier found for result {result.verifier_id}")
            continue

        numerical_weight = verifier.verifier_values.get("numerical_weight")
        if numerical_weight is None:
            numerical_weight = 1.0
        else:
            numerical_weight = float(numerical_weight)

        if numerical_weight != 0:
            weighted_sum += result.score * numerical_weight
            if numerical_weight > 0:
                total_weights += numerical_weight
            verifier_count += 1
            logger.debug(
                f"[SCORING] verifier={result.verifier_id} | score={result.score} | weight={numerical_weight}"
            )
        else:
            logger.debug(
                f"[SCORING] verifier={result.verifier_id} | skipped (weight=0)"
            )

        passed = result.score >= pass_score_threshold

        if expert_assessment_values and _has_value(verifier, expert_assessment_values):
            expert_total += 1
            if passed:
                expert_passed += 1

        if not passed and gate_caps:
            for gate_value, cap in _matched_gate_caps(verifier, gate_caps):
                triggered_gates.append(
                    {
                        "verifier_id": result.verifier_id,
                        "gate_value": gate_value,
                        "cap": cap,
                        "score": result.score,
                    }
                )

    base_score = weighted_sum / total_weights if total_weights > 0 else 0.0
    base_score = max(0.0, min(1.0, base_score))

    triggered_caps: list[float] = []

    expert_pass_rate: float | None = None
    expert_floor_triggered = False
    if expert_total > 0:
        expert_pass_rate = expert_passed / expert_total
        if expert_pass_rate < expert_assessment_pass_rate_threshold:
            triggered_caps.append(expert_assessment_floor_cap)
            expert_floor_triggered = True

    for gate in triggered_gates:
        triggered_caps.append(gate["cap"])

    final_score = min(base_score, *triggered_caps) if triggered_caps else base_score
    final_score = max(0.0, min(1.0, final_score))

    logger.info(
        f"[SCORING] Deep Research Weighted Average + Gates | "
        f"final_score={final_score:.4f} | base_score={base_score:.4f} | "
        f"expert_total={expert_total} expert_passed={expert_passed} "
        f"expert_floor_triggered={expert_floor_triggered} | "
        f"gate_failures={len(triggered_gates)}"
    )

    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "base_score": base_score,
            "weighted_sum": weighted_sum,
            "total_weights": total_weights,
            "verifier_count": verifier_count,
            "expert_assessment_total": expert_total,
            "expert_assessment_passed": expert_passed,
            "expert_assessment_pass_rate": expert_pass_rate,
            "expert_assessment_floor_triggered": expert_floor_triggered,
            "triggered_gates": triggered_gates,
            "triggered_caps": triggered_caps,
        },
    )
