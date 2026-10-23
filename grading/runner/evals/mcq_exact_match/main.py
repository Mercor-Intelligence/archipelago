"""MCQ Exact Match eval for multiple-choice benchmarks (e.g. MMLU-Pro).

Extracts the model's answer letter (A–J) from its final response using the
same two-pass extraction logic as the MMLU-Pro OSS harness (compute_accuracy.py):
  1. Regex: "answer is (X)" or "answer is (X)"
  2. Fallback: "Answer: X"
  3. Final fallback: last standalone A–J letter in the response

Source: https://github.com/TIGER-AI-Lab/MMLU-Pro/blob/main/compute_accuracy.py
"""

import re

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult
from runner.utils.trajectory import extract_final_assistant_response

CHOICE_LETTERS = set("ABCDEFGHIJ")


def _extract_answer(text: str) -> str | None:
    """Mirror MMLU-Pro OSS extract_answer (level 'l2') from compute_accuracy.py."""
    match = re.search(r"answer is \(?([A-J])\)?", text)
    if match:
        return match.group(1)
    return _extract_again(text)


def _extract_again(text: str) -> str | None:
    """Mirror MMLU-Pro OSS extract_again from compute_accuracy.py."""
    match = re.search(r".*[aA]nswer:\s*([A-J])", text)
    if match:
        return match.group(1)
    return _extract_final(text)


def _extract_final(text: str) -> str | None:
    """Mirror MMLU-Pro OSS extract_final from compute_accuracy.py.

    Returns the last standalone A–J letter in the response.
    """
    match = re.search(r"\b[A-J]\b(?!.*\b[A-J]\b)", text, re.DOTALL)
    if match:
        return match.group(0)
    return None


async def mcq_exact_match_eval(input: EvalImplInput) -> VerifierResult:
    """Grade a single MCQ question by exact match on the extracted answer letter."""
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    correct_answer = verifier_values.get("mcq_correct_answer", "")
    if not correct_answer:
        raise ValueError("Missing required field: mcq_correct_answer")
    correct_answer = correct_answer.strip().upper()
    if correct_answer not in CHOICE_LETTERS:
        raise ValueError(
            f"mcq_correct_answer must be a single letter A–J, got: {correct_answer!r}"
        )

    final_response = extract_final_assistant_response(input)
    predicted = _extract_answer(final_response) if final_response else None
    passed = predicted is not None and predicted.upper() == correct_answer

    logger.info(
        f"[MCQ_EXACT_MATCH] task={task_id} | correct={correct_answer} predicted={predicted} passed={passed}"
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values={
            "passed": passed,
            "predicted_answer": predicted or "",
            "correct_answer": correct_answer,
            "rationale": (
                f"Model answered {predicted!r}, correct answer is {correct_answer!r}"
                if predicted
                else f"Could not extract answer from response; correct answer is {correct_answer!r}"
            ),
        },
    )
