"""MRCR Similarity eval - prefix check + SequenceMatcher for needle-in-haystack benchmarks."""

from difflib import SequenceMatcher

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus


async def mrcr_similarity_eval(input: EvalImplInput) -> VerifierResult:
    """Grade an MRCR (Multi-Round Co-reference Resolution) response.

    Checks that the response starts with the expected hash prefix, then
    computes a SequenceMatcher similarity ratio between the stripped
    response and the expected answer.

    Verifier values:
        expected_prefix: The alphanumeric string the model must prepend.
        expected_answer: The full expected answer (including the prefix).
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    expected_prefix = verifier_values.get("expected_prefix", "")
    expected_answer = verifier_values.get("expected_answer", "")

    if not expected_prefix:
        raise ValueError("expected_prefix is required in verifier_values")
    if not expected_answer:
        raise ValueError("expected_answer is required in verifier_values")

    # Get the model's final answer
    final_answer = ""
    if input.helper_results:
        final_answer = str(input.helper_results.get(HelperIds.FINAL_ANSWER) or "")

    logger.info(
        f"[MRCR_SIMILARITY] task={task_id} | "
        f"prefix={expected_prefix} | "
        f"response_len={len(final_answer)} | "
        f"expected_len={len(expected_answer)}"
    )

    # Check prefix
    has_prefix = final_answer.startswith(expected_prefix)

    if not has_prefix:
        logger.info(
            f"[MRCR_SIMILARITY] task={task_id} | "
            f"FAIL: missing prefix (response starts with {final_answer[:20]!r})"
        )
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.OK,
            verifier_result_values={
                "has_prefix": False,
                "similarity_score": 0.0,
                "rationale": "Response does not start with the expected hash prefix.",
            },
        )

    # Strip prefix and compute similarity
    response_stripped = final_answer.removeprefix(expected_prefix)
    answer_stripped = expected_answer.removeprefix(expected_prefix)
    similarity = SequenceMatcher(None, response_stripped, answer_stripped).ratio()

    logger.info(
        f"[MRCR_SIMILARITY] task={task_id} | prefix=OK | similarity={similarity:.4f}"
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=similarity,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "has_prefix": True,
            "similarity_score": similarity,
            "rationale": f"Prefix correct. Similarity: {similarity:.4f}",
        },
    )
