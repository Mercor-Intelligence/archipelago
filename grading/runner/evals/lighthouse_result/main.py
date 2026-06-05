import math
from collections.abc import Mapping
from typing import cast

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus


def _coerce_score(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        score = float(value)
        return score if math.isfinite(score) else None
    if isinstance(value, str):
        try:
            score = float(value)
        except ValueError:
            return None
        return score if math.isfinite(score) else None
    return None


async def lighthouse_result_eval(input: EvalImplInput) -> VerifierResult:
    output = input.trajectory.output or {}
    eval_status = output.get("eval_status")
    if eval_status == "failed":
        harness_error = output.get("error_message")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "error": "Lighthouse harness failed before agent could run",
                "harness_error": harness_error,
                "eval_status": eval_status,
            },
            message=(f"Lighthouse harness failed: {harness_error or 'unknown error'}"),
        )

    score = _coerce_score(output.get("score"))

    if score is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"error": "Missing Lighthouse score"},
            message="Missing Lighthouse score in trajectory output",
        )
    metadata = output.get("test_summary_metadata")
    metadata_values: Mapping[str, object] = (
        cast(Mapping[str, object], metadata) if isinstance(metadata, Mapping) else {}
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=score,
        verifier_result_values={
            "lighthouse_score": score,
            "eval_status": eval_status,
            "f2p_passed": metadata_values.get("f2p_passed"),
            "f2p_total": metadata_values.get("f2p_total"),
            "p2p_passed": metadata_values.get("p2p_passed"),
            "p2p_total": metadata_values.get("p2p_total"),
            "tests_total": output.get("tests_total"),
            "tests_passed": output.get("tests_passed"),
            "tests_failed": output.get("tests_failed"),
            "tests_skipped": output.get("tests_skipped"),
            "exit_code": output.get("exit_code"),
            "duration_seconds": output.get("duration_seconds"),
            "test_statuses": output.get("test_statuses"),
            "fail_to_pass_results": metadata_values.get("fail_to_pass_results"),
            "pass_to_pass_results": metadata_values.get("pass_to_pass_results"),
        },
        message=f"Lighthouse eval complete: status={eval_status}, score={score}",
    )
