"""Deep Research Weighted Average scoring method with gate-based caps.

Computes the same weighted average as ``deep_research_weighted_average`` and
then applies up to three kinds of post-hoc score adjustments driven by
verifier custom fields:

1. **Expert Assessment floor (Gate 1).** If fewer than
   ``expert_assessment_pass_rate_threshold`` of verifiers tagged with one of
   ``expert_assessment_values`` pass (score >= ``pass_score_threshold``), the
   final score is capped at ``expert_assessment_floor_cap``.

2. **Custom Gate failures (Gates 2/3).** Any verifier whose custom field value
   matches a key in ``gate_caps`` triggers the corresponding cap if that
   verifier fails (score < ``pass_score_threshold``). When multiple gates fire,
   the lowest cap wins.

3. **Critical Value multiplicative haircut (Workbench Goodhart's-law guard).**
   After the cap layer above settles on ``capped_score``, each failed verifier
   tagged with a ``critical_value_field_values`` entry (default
   ``"Gate: Critical Value"``) compounds a multiplicative penalty:
   ``final = capped_score × critical_value_multiplier ** N``, where ``N`` is
   the number of failed Critical Value verifiers and ``critical_value_multiplier``
   defaults to ``0.8``. Set ``critical_value_field_values=[]`` to disable.

Custom field values are matched by *value* (not field id), so the same scoring
config works across eval configs that label things consistently. Defaults
target the Atlas world conventions (``"Expert Assessment"``,
``"Gate: Missing Scope"``, ``"Gate: Ethical / Safety Violation"``,
``"Gate: Critical Value"``) but every threshold, cap, and multiplier is
overridable per scoring config.
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
DEFAULT_CRITICAL_VALUE_FIELD_VALUES = ["Gate: Critical Value"]
DEFAULT_CRITICAL_VALUE_MULTIPLIER = 0.8


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

    critical_value_field_values: set[str] = set(
        scoring_config_values.get(
            "critical_value_field_values", DEFAULT_CRITICAL_VALUE_FIELD_VALUES
        )
        or []
    )
    critical_value_multiplier = float(
        scoring_config_values.get(
            "critical_value_multiplier", DEFAULT_CRITICAL_VALUE_MULTIPLIER
        )
    )

    verifier_map = {v.verifier_id: v for v in verifiers}

    weighted_sum = 0.0
    total_weights = 0.0
    verifier_count = 0

    expert_total = 0
    expert_passed = 0
    triggered_gates: list[dict[str, Any]] = []
    critical_value_total = 0
    failed_critical_values: list[dict[str, Any]] = []

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

        # Critical Value tagging is independent of gate_caps — a verifier can
        # be both a (capped) gate and a (multiplicative) critical value. We
        # accumulate the failures here and apply the haircut after the cap
        # layer settles below.
        if critical_value_field_values:
            matched_critical_values = [
                v
                for v in _verifier_field_values(verifier)
                if isinstance(v, str) and v in critical_value_field_values
            ]
            if matched_critical_values:
                critical_value_total += 1
                if not passed:
                    failed_critical_values.append(
                        {
                            "verifier_id": result.verifier_id,
                            # Match by first hit; rubrics typically tag each
                            # verifier with at most one Critical Value entry.
                            "field_value": matched_critical_values[0],
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

    score_before_critical_value = (
        min(base_score, *triggered_caps) if triggered_caps else base_score
    )
    score_before_critical_value = max(0.0, min(1.0, score_before_critical_value))

    # Multiplicative haircut compounds per failed Critical Value verifier.
    # Negative multipliers are nonsensical and a multiplier of 1.0 is a no-op.
    critical_value_failed = len(failed_critical_values)
    critical_value_multiplier_applied = (
        critical_value_multiplier**critical_value_failed
        if critical_value_failed > 0
        else 1.0
    )
    final_score = score_before_critical_value * critical_value_multiplier_applied
    final_score = max(0.0, min(1.0, final_score))

    logger.info(
        f"[SCORING] Deep Research Weighted Average + Gates | "
        f"final_score={final_score:.4f} | "
        f"score_before_critical_value={score_before_critical_value:.4f} | "
        f"base_score={base_score:.4f} | "
        f"expert_total={expert_total} expert_passed={expert_passed} "
        f"expert_floor_triggered={expert_floor_triggered} | "
        f"gate_failures={len(triggered_gates)} | "
        f"critical_value_failed={critical_value_failed} "
        f"critical_value_multiplier_applied={critical_value_multiplier_applied:.4f}"
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
            "critical_value_total": critical_value_total,
            "critical_value_failed": critical_value_failed,
            "critical_value_multiplier_applied": critical_value_multiplier_applied,
            "score_before_critical_value": score_before_critical_value,
            "failed_critical_values": failed_critical_values,
        },
    )
