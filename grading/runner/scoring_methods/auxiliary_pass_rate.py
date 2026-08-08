from typing import Any

from loguru import logger

from runner.models import (
    ScoringMethodResult,
    Verifier,
    VerifierResult,
    VerifierResultStatus,
)
from runner.scoring_methods.utils import format_verifier_errors


async def auxiliary_pass_rate_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    scoring_config_values: dict[str, Any],
) -> ScoringMethodResult:
    verifier_errors = [
        result
        for result in verifier_results
        if result.status == VerifierResultStatus.ERROR
    ]
    if verifier_errors:
        error_msg = format_verifier_errors(verifier_errors, verifiers)
        logger.error(error_msg)
        raise ValueError(error_msg)

    threshold = float(scoring_config_values.get("pass_threshold", 0.99))
    evaluation_target = scoring_config_values.get("evaluation_target")
    if evaluation_target:
        verifier_keys = {
            (verifier.verifier_id, verifier.verifier_version)
            for verifier in verifiers
            if verifier.evaluation_target.value == evaluation_target
        }
        verifier_results = [
            result
            for result in verifier_results
            if (result.verifier_id, result.verifier_version) in verifier_keys
        ]

    total_count = len(verifier_results)
    if total_count == 0:
        raise ValueError("Auxiliary pass rate requires at least one verifier result")
    passed_count = sum(1 for result in verifier_results if result.score >= threshold)
    failed_count = total_count - passed_count
    final_score = passed_count / total_count if total_count else 0.0
    return ScoringMethodResult(
        final_score=final_score,
        scoring_method_result_values={
            "passed_count": passed_count,
            "failed_count": failed_count,
            "total_count": total_count,
            "pass_threshold": threshold,
            "final_score_percentage": final_score * 100,
        },
    )
