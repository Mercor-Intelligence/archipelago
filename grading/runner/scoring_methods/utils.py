from runner.models import Verifier, VerifierResult
from runner.utils.metrics import increment


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
    verifier_map = {v.verifier_id: v for v in verifiers}
    error_lines: list[str] = []

    for vr in verifier_errors:
        verifier = verifier_map.get(vr.verifier_id)
        rubric_num = verifier.verifier_index + 1 if verifier else "?"

        error_lines.append(f"- Rubric Item #{rubric_num}: {vr.message[:100]}")

        increment(
            "grading.verifier.error",
            tags=[f"rubric_item:{rubric_num}"],
        )

    header = f"Cannot compute score: {len(verifier_errors)} verifier(s) had errors:"
    return f"{header}\n" + "\n".join(error_lines)
