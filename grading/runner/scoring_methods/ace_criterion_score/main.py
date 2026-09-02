"""ACE Criterion Score scoring method with hurdle logic.

Scoring logic per the ACE specification:
1. Check hurdles - if ANY hurdle criterion fails (score <= 0), final score = 0
2. Non-grounded criteria: met = +1, not met = 0
3. Grounded criteria:
   - Stage 1 fail (criterion NOT met in response) = 0
   - Stage 1 pass + Stage 2 pass (verified in sources) = +1
   - Stage 1 pass + Stage 2 fail (hallucination) = -1

Total score is sum of all criterion scores.
Hurdle-adjusted score is 0 if any hurdle fails, else total_score.
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


async def ace_criterion_score_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    """
    Calculate ACE score with hurdle logic.

    Hurdle Logic:
    - If ALL hurdle criteria pass (score = 1), proceed with normal scoring
    - If ANY hurdle criterion fails (score <= 0), the entire task score is 0

    Score Mapping:
    - Verifier score >= 0.99 → ACE score = 1 (pass)
    - Verifier score >= 0.0 and < 0.5 → ACE score = 0 (fail response text)
    - Verifier score < 0.0 → ACE score = -1 (fail source verification / hallucination)

    Args:
        verifier_results: Results from ACE criterion verifiers
        verifiers: Verifier configs with hurdle_tag in verifier_values
        scoring_config_values: Configuration (currently unused)

    Returns:
        ScoringMethodResult with:
        - final_score: Hurdle-adjusted score (0 if any hurdle fails)
        - total_score: Sum of all ACE criterion scores (before hurdle adjustment)
        - Various counts and breakdown
    """
    verifier_errors = [
        vr for vr in verifier_results if vr.status == VerifierResultStatus.ERROR
    ]
    if verifier_errors:
        error_msg = format_verifier_errors(verifier_errors, verifiers)
        logger.error(error_msg)
        raise ValueError(error_msg)

    verifier_map = {v.verifier_id: v for v in verifiers}

    task_results = [
        r
        for r in verifier_results
        if verifier_map.get(r.verifier_id)
        and verifier_map[r.verifier_id].task_id is not None
    ]

    if not task_results:
        task_results = verifier_results

    ace_scores: list[int] = []
    hurdle_scores: list[int] = []
    criteria_details: list[dict[str, Any]] = []

    for result in task_results:
        verifier = verifier_map.get(result.verifier_id)
        if not verifier:
            continue

        hurdle_tag = (verifier.verifier_values or {}).get("hurdle_tag", "Not")

        if result.score >= 0.99:
            ace_score = 1
        elif result.score >= 0.0:
            ace_score = 0
        else:
            ace_score = -1

        ace_scores.append(ace_score)

        if hurdle_tag == "Hurdle":
            hurdle_scores.append(ace_score)

        criteria_details.append(
            {
                "verifier_id": result.verifier_id,
                "raw_score": result.score,
                "ace_score": ace_score,
                "hurdle_tag": hurdle_tag,
                "description": (result.verifier_result_values or {}).get(
                    "description", ""
                )[:80],
            }
        )

    total_score = sum(ace_scores)
    pass_count = sum(1 for s in ace_scores if s == 1)
    fail_response_count = sum(1 for s in ace_scores if s == 0)
    fail_source_count = sum(1 for s in ace_scores if s == -1)

    hurdle_count = len(hurdle_scores)
    hurdle_pass_count = sum(1 for s in hurdle_scores if s == 1)
    hurdle_all_pass = all(s == 1 for s in hurdle_scores) if hurdle_scores else True

    if not hurdle_scores:
        total_hurdle_score = total_score
    elif hurdle_all_pass:
        total_hurdle_score = total_score
    else:
        total_hurdle_score = 0

    total_count = len(ace_scores)
    if total_count > 0:
        normalized_score = max(0.0, total_hurdle_score / total_count)
    else:
        normalized_score = 0.0

    logger.info(
        f"[ACE_CRITERION_SCORE] "
        f"total={total_score} | hurdle_adjusted={total_hurdle_score} | "
        f"pass={pass_count} fail_response={fail_response_count} fail_source={fail_source_count} | "
        f"hurdles={hurdle_pass_count}/{hurdle_count} ({'PASS' if hurdle_all_pass else 'FAIL'})"
    )

    for detail in criteria_details:
        score = detail["ace_score"]
        if score == 1:
            status = "PASS"
        elif score == 0:
            status = "FAIL(resp)"
        else:
            status = "FAIL(src)"
        hurdle = "Hurdle" if detail["hurdle_tag"] == "Hurdle" else "  "
        logger.debug(f"  {hurdle} {status}: {detail['description']}...")

    return ScoringMethodResult(
        final_score=normalized_score,
        scoring_method_result_values={
            "total_score": total_score,
            "total_hurdle_score": total_hurdle_score,
            "pass_count": pass_count,
            "fail_response_count": fail_response_count,
            "fail_source_count": fail_source_count,
            "hurdle_count": hurdle_count,
            "hurdle_pass_count": hurdle_pass_count,
            "hurdle_all_pass": hurdle_all_pass,
            "total_count": total_count,
            "normalized_score": normalized_score,
            "criteria_details": criteria_details,
        },
    )
