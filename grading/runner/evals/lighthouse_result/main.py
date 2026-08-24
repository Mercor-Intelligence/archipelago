import math
from collections.abc import Mapping
from typing import cast

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus

# CODE-1077: the lighthouse harness tags a timed-out trial with this outcome. Harbor
# runs the verifier after an agent timeout, so such a trajectory still arrives with
# eval_status "completed" and a real (low) score; grade it as a harness ERROR so it is
# not read as a genuine score-0 completion.
_AGENT_TIMEOUT_OUTCOME = "agent_timeout"


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

    # A provider content-filter refusal -- either stopped at the first refusal by the
    # guard (content_filter_refusal), or looped to the step limit on a run recorded
    # before the guard existed / on a lane it cannot reach (empty_response_loop).
    #
    # Graded as a genuine model failure: score 0.0, status OK -- NOT a harness ERROR.
    # Team decision (2026-08-14, #eval-flywheel): a refusal is the model's own
    # behaviour and belongs in the score rather than being excluded from it. The flags
    # keep it filterable for anyone analysing refusal rate separately.
    #
    # The 0 is ASSERTED, not read. harbor only continues to the verifier phase for
    # AgentTimeoutError / NonZeroAgentExitCodeError (trial/single_step.py:83), so a
    # guard-stopped trial has no verifier result -- without this branch it would fall
    # through to "Missing Lighthouse score" and be excluded, which is the opposite of
    # counting it. Asserting 0 is sound: the model emitted nothing, so nothing it did
    # could pass, and both observed looped trajectories scored 0 from the verifier too.
    #
    # A guard-stopped trial reaches this code only because the agent promotes it from
    # FAILED to COMPLETED (lighthouse_code_agent/main.py:_assemble_output) -- Studio
    # queues grading for COMPLETED alone. Leave that promotion in place or this branch
    # goes back to being dead code for the shape it was written for.
    #
    # Checked FIRST -- ahead of both the eval_status and timeout branches:
    #
    #  * ahead of eval_status: a guard-stopped trial has an exception and no verifier
    #    result, which is exactly the condition the harbor adapter maps to
    #    EvalStatus.FAILED (harnesses/harbor/adapter.py:3352-3357). Behind that branch
    #    this code is unreachable for the very trials it exists for, and they keep
    #    being excluded as "harness failed before the agent could run" -- which is
    #    also untrue. Caught in review by both Devin and Cursor.
    #  * ahead of the timeout branch: a refusal loop can exhaust the agent budget and
    #    be recorded as an ordinary timeout, so the root cause must win over the
    #    symptom.
    #
    # A genuine harness failure sets neither flag, so it still falls through to the
    # eval_status branch below.
    if output.get("content_filter_refusal") or output.get("empty_response_loop"):
        after_output = bool(output.get("content_filter_after_output"))
        stopped_early = bool(output.get("content_filter_refusal")) and not after_output
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "lighthouse_score": 0.0,
                "eval_status": eval_status,
                "content_filter_refusal": bool(output.get("content_filter_refusal")),
                "content_filter_after_output": after_output,
                "empty_response_loop": bool(output.get("empty_response_loop")),
                # The score harbor actually recorded, surfaced BESIDE the asserted 0
                # rather than dropped. Non-null mainly on a mid-run refusal, where a
                # real test_summary can exist (see _content_filter_after_output).
                #
                # Decided 2026-08-14: the grade stays 0 even when this holds a genuine
                # measurement. A refusal is a model failure regardless of which turn it
                # lands on, so the verdict does not depend on how far the run got.
                # Review (Devin) recommended preserving the measured score here, and
                # ERROR-ing it as a truncated run was also considered -- rejected
                # because that excludes the row, which is the outcome the team
                # explicitly did not want. Keeping the value visible is what makes the
                # override auditable rather than a silent discard; do not "fix" this
                # into preserving the score without the eval-flywheel team's call.
                "recorded_score": output.get("score"),
                "exception_type": output.get("exception_type"),
                "exception_message": output.get("exception_message"),
                # What the verifier recorded, when it ran at all -- absent on a
                # guard-stopped trial. Kept so the asserted 0 can be reconciled.
                "tests_total": output.get("tests_total"),
                "tests_passed": output.get("tests_passed"),
            },
            message=(
                "Provider declined the request (content filter) — scored 0 as a model "
                "failure. "
                + (
                    # Accuracy matters here: an operator triaging the row reads this,
                    # and "the model produced no output" is simply untrue of a refusal
                    # that arrived mid-run. (Devin review.)
                    f"The model produced output first, then was declined mid-run; the "
                    f"recorded score ({output.get('score')}) is superseded."
                    if after_output
                    else "Trial stopped at the first refusal by the refusal guard."
                    if stopped_early
                    else "The harness looped on the empty response to its step limit."
                )
            ),
        )

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

    if output.get("outcome") == _AGENT_TIMEOUT_OUTCOME:
        exception_message = output.get("exception_message")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "outcome": _AGENT_TIMEOUT_OUTCOME,
                "exception_type": output.get("exception_type"),
                "exception_message": exception_message,
                "eval_status": eval_status,
            },
            message=(
                f"Lighthouse trial timed out: {exception_message or 'agent timeout'}"
            ),
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
            # GP-validation runs only; None for normal execution runs.
            "validation_passed": output.get("validation_passed"),
            "empty_score": output.get("empty_score"),
            "golden_score": output.get("golden_score"),
        },
        message=f"Lighthouse eval complete: status={eval_status}, score={score}",
    )
