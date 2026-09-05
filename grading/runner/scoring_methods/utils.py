from runner.models import Verifier, VerifierResult
from runner.utils.metrics import increment
from runner.utils.verifier_display import build_display_positions

# Cap each verifier's message generously rather than hard-truncating at a
# tiny length: the previous 100-char cap silently swallowed the actual error
# detail (e.g. a Pydantic ValidationError like "...1 validation error for
# FooVerdict\n  Invalid JSON: <parse detail>" got cut down to just "Invalid",
# with the parse detail that would actually explain the failure discarded).
# 2000 chars comfortably fits a full validation error or exception repr while
# still bounding a pathological one (e.g. an embedded raw completion).
MAX_VERIFIER_MESSAGE_LENGTH = 2000


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
