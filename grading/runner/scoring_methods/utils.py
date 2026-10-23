import math

from loguru import logger

from runner.models import Verifier, VerifierResult
from runner.utils.metrics import increment
from runner.utils.verifier_display import build_display_positions

DEFAULT_WEIGHT_VERIFIER_VALUE_ID = "numerical_weight"
NATIVE_WEIGHT_VERIFIER_VALUE_ID = "weight"

# Cap each verifier's message generously rather than hard-truncating at a
# tiny length: the previous 100-char cap silently swallowed the actual error
# detail (e.g. a Pydantic ValidationError like "...1 validation error for
# FooVerdict\n  Invalid JSON: <parse detail>" got cut down to just "Invalid",
# with the parse detail that would actually explain the failure discarded).
# 2000 chars comfortably fits a full validation error or exception repr while
# still bounding a pathological one (e.g. an embedded raw completion).
MAX_VERIFIER_MESSAGE_LENGTH = 2000


def resolve_verifier_weight(
    verifier: Verifier,
    weight_custom_field_id: str | None = None,
    weight_verifier_value_id: str | None = None,
) -> float:
    """Resolve a verifier's numeric weight.

    Priority:
      1. verifier.verifier_custom_field_values[weight_custom_field_id] when
         the scoring config opts in via ``weight_custom_field_id`` and the
         field is set + parseable as a finite float.
      2. verifier.verifier_values[weight_verifier_value_id] when configured,
         set, and parseable as a finite float.
      3. verifier.verifier_values["numerical_weight"] when set + parseable.
      4. verifier.verifier_values["weight"], the native field, when set +
         parseable.
      5. 1.0 (unweighted average) as the final fallback.

    Non-numeric, non-finite, None and empty values fall through to the next
    layer rather than crashing the run or poisoning the average, so a
    misconfigured field_id cannot wedge scoring.
    """
    return resolve_verifier_weight_with_source(
        verifier, weight_custom_field_id, weight_verifier_value_id
    )[0]


def resolve_verifier_weight_with_source(
    verifier: Verifier,
    weight_custom_field_id: str | None = None,
    weight_verifier_value_id: str | None = None,
) -> tuple[float, str]:
    if weight_custom_field_id:
        cf_values = verifier.verifier_custom_field_values or {}
        raw = cf_values.get(weight_custom_field_id)
        if raw is not None and raw != "":
            weight = _parse_finite_float(raw)
            if weight is not None:
                return weight, f"verifier_custom_field_values.{weight_custom_field_id}"
            logger.warning(
                f"[SCORING] verifier={verifier.verifier_id} | "
                f"unusable custom weight {raw!r} for field_id="
                f"{weight_custom_field_id}; falling back"
            )

    verifier_value_ids = [
        weight_verifier_value_id or DEFAULT_WEIGHT_VERIFIER_VALUE_ID,
        DEFAULT_WEIGHT_VERIFIER_VALUE_ID,
        NATIVE_WEIGHT_VERIFIER_VALUE_ID,
    ]

    seen: set[str] = set()
    for field_id in verifier_value_ids:
        if field_id in seen:
            continue
        seen.add(field_id)
        raw = verifier.verifier_values.get(field_id)
        if raw is None or raw == "":
            continue
        weight = _parse_finite_float(raw)
        if weight is not None:
            return weight, f"verifier_values.{field_id}"
        logger.warning(
            f"[SCORING] verifier={verifier.verifier_id} | "
            f"unusable verifier weight {raw!r} for field_id={field_id}; "
            "falling back"
        )

    return 1.0, "default"


def _parse_finite_float(raw: float | str) -> float | None:
    """Parse ``raw`` as a finite float, or return None.

    ``float()`` accepts "Infinity" and "nan", which cannot form a weighted
    average: an infinite weight against a zero score yields NaN.
    """
    try:
        weight = float(raw)
    except (TypeError, ValueError):
        return None
    return weight if math.isfinite(weight) else None


def format_verifier_errors(
    verifier_errors: list[VerifierResult],
    verifiers: list[Verifier],
) -> str:
    """
    Format verifier errors for logging.

    Args:
        verifier_errors: List of VerifierResult objects with errors
        verifiers: List of Verifier objects

    Returns:
        Formatted error message
    """
    display_position = build_display_positions(verifiers)

    error_lines: list[str] = []

    for vr in verifier_errors:
        rubric_num = display_position.get(vr.verifier_id, "?")

        message = vr.message
        if len(message) > MAX_VERIFIER_MESSAGE_LENGTH:
            message = (
                f"{message[:MAX_VERIFIER_MESSAGE_LENGTH]}... "
                f"[truncated, {len(message)} chars total]"
            )

        error_lines.append(f"- Rubric Item #{rubric_num}: {message}")

        increment(
            "grading.verifier.error",
            tags=[f"rubric_item:{rubric_num}"],
        )

    header = f"Cannot compute score: {len(verifier_errors)} verifier(s) had errors:"
    return f"{header}\n" + "\n".join(error_lines)
