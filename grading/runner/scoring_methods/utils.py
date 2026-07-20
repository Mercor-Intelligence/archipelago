from runner.models import Verifier, VerifierResult
from runner.utils.metrics import increment
from runner.utils.verifier_display import build_display_positions


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

        error_lines.append(f"- Rubric Item #{rubric_num}: {vr.message[:100]}")

        increment(
            "grading.verifier.error",
            tags=[f"rubric_item:{rubric_num}"],
        )

    header = f"Cannot compute score: {len(verifier_errors)} verifier(s) had errors:"
    return f"{header}\n" + "\n".join(error_lines)
