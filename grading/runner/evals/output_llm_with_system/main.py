"""LLM Judge eval implementing AdvancedIF IFRubricsJudge.

For if_carried_context_oss (multi-turn) and if_complex_if_oss (single-turn, no system)
tasks. The actual LLM call is made once per task in the IF_JUDGE_RESULT helper;
this function just reads the cached per-criterion result.
"""

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult


async def llm_judge_with_system_eval(input: EvalImplInput) -> VerifierResult:
    """Read per-criterion result from the IF_JUDGE_RESULT batch helper."""
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"
    criteria = verifier_values.get("criteria", "")

    if not criteria:
        raise ValueError("Missing required field: criteria")

    batch_results = (input.helper_results or {}).get(HelperIds.IF_JUDGE_RESULT) or {}
    result = batch_results.get(criteria)

    if result is None:
        raise ValueError(
            f"No batch result for criteria (task={task_id}): {criteria[:80]}"
        )

    logger.info(
        f"[JUDGE][IF][RESULT] task={task_id} | result={result.judge_grade.upper()} | "
        f"criteria={criteria[:80]}"
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if result.judge_grade == "pass" else 0.0,
        verifier_result_values={
            "judge_grade": result.judge_grade,
            "grade_rationale": result.grade_rationale,
            "satisfied_all_requirements": result.satisfied_all_requirements,
            "rubrics_check": result.rubrics_check,
            "rubrics_passed": result.rubrics_passed,
        },
    )
