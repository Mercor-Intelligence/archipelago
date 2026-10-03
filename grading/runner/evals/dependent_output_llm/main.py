"""Output LLM judge that withholds credit when a prerequisite criterion failed.

Opt-in per criterion: nothing else reads ``verifier_dependencies`` for scoring.
"""

from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.judge_relay import active_relay

# Relative, and pointing at `.main`, because `test_eval_export_types.py` decides
# whether an eval calls an LLM by walking its relative import closure.
from ..output_llm.main import llm_judge_eval

GATED_KEY = "gated"
GATED_BY_KEY = "gated_by"
_JUDGE_GRADE_KEY = "judge_grade"

# None of these is a model failure, so none may gate a dependent.
_NO_VERDICT_KEYS = ("verifier_crashed", "skipped", "ungradeable")


def _has_verdict(result: VerifierResult) -> bool:
    """Whether this prerequisite produced a verdict that can be judged pass/fail."""
    if result.status == VerifierResultStatus.ERROR:
        return False
    values: dict[str, Any] = result.verifier_result_values or {}
    return not any(values.get(key) for key in _NO_VERDICT_KEYS)


def _passed(result: VerifierResult) -> bool:
    """Whether a prerequisite counts as satisfied.

    Score is the authority; ``judge_grade`` only catches a row that scored above
    zero while its own judge called it a fail.
    """
    if (result.verifier_result_values or {}).get(_JUDGE_GRADE_KEY) == "fail":
        return False
    return result.score > 0


def unpassed_dependency_ids(dependencies: list[VerifierResult] | None) -> list[str]:
    """Prerequisite ids that produced a verdict and did not pass, in input order."""
    return [
        dep.verifier_id
        for dep in (dependencies or [])
        if _has_verdict(dep) and not _passed(dep)
    ]


def gated_result(
    verifier_id: str, version: int, failed_ids: list[str]
) -> VerifierResult:
    """An uncredited result: score 0, status OK, and still counted by scoring."""
    reason = (
        f"Not credited: this criterion depends on {', '.join(failed_ids)}, "
        "which did not pass."
    )
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=version,
        score=0.0,
        verifier_result_values={
            GATED_KEY: True,
            GATED_BY_KEY: failed_ids,
            "result": 0,
            _JUDGE_GRADE_KEY: "fail",
            "reason": reason,
        },
        status=VerifierResultStatus.OK,
        message=reason,
    )


async def dependent_output_llm_eval(input: EvalImplInput) -> VerifierResult:  # noqa: A002
    """Grade with the output LLM judge unless a prerequisite criterion failed."""
    verifier = input.verifier

    # A relay collect pass answers every judge with a forced-failure stub, so a
    # prerequisite reads as failed before its real verdict exists. Gating there
    # collects no prompt for this criterion, and nothing downstream can undo it.
    if active_relay() is not None:
        logger.warning(
            f"[DEPENDENT_OUTPUT_LLM] verifier {verifier.verifier_id} graded under "
            "client relay; dependency gating does not apply to a relayed run"
        )
        return await llm_judge_eval(input)

    failed_ids = unpassed_dependency_ids(input.dependencies)
    if not failed_ids:
        return await llm_judge_eval(input)

    logger.info(
        f"[DEPENDENT_OUTPUT_LLM] task={verifier.task_id or 'unknown'} | "
        f"verifier {verifier.verifier_id} not credited; unpassed "
        f"dependencies: {', '.join(failed_ids)}"
    )
    return gated_result(verifier.verifier_id, verifier.verifier_version, failed_ids)
