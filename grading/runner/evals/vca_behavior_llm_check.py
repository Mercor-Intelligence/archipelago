import json
from typing import Any

from runner.evals.models import EvalImplInput
from runner.evals.output_llm.utils.prompts import (
    JSON_OUTPUT_GRADING,
    RATIONALE_FORMAT_BASIC,
    STRICT_CRITERION_MATCHING,
    TOLERANCE_NOTES,
)
from runner.evals.output_llm.utils.services.grading_judge import run_grading_judge
from runner.helpers.models import HelperIds
from runner.helpers.vca_context import VcaContext
from runner.models import EvaluationTarget, VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages, call_llm

_VCA_BEHAVIOR_SYSTEM_BASE = """<CONTEXT>
You are grading a Virtual Coworker Agent (VCA), not the Target Agent.

The VCA is a simulated coworker inside an Archipelago environment. Grade whether the VCA behavior satisfies the criterion. Use only the supplied trajectory, prompt, event, and coordinator evidence.

Do not grade the target agent's task performance. Only grade the VCA behavior selected by vca_id.
</CONTEXT>"""

VCA_BEHAVIOR_SYSTEM_PROMPT = "\n\n".join(
    [
        _VCA_BEHAVIOR_SYSTEM_BASE,
        STRICT_CRITERION_MATCHING,
        TOLERANCE_NOTES,
        RATIONALE_FORMAT_BASIC,
        JSON_OUTPUT_GRADING,
    ]
)

VCA_BEHAVIOR_TIMEOUT_SECONDS = 90
MAX_JSON_RETRIES = 3


async def vca_behavior_llm_check_eval(input: EvalImplInput) -> VerifierResult:
    verifier_values = input.verifier.verifier_values or {}
    criteria = str(verifier_values.get("criteria") or "").strip()
    if not criteria:
        raise ValueError("Missing required field: criteria")
    if input.trajectory.evaluation_target != EvaluationTarget.VIRTUAL_COWORKER_AGENT:
        raise ValueError(
            "VCA behavior eval can only run on virtual_coworker_agent grading runs"
        )
    if not input.trajectory.vca_id:
        raise ValueError("VCA behavior eval requires vca_id trajectory metadata")

    vca_context = _get_vca_context(input)
    model = input.grading_settings.llm_judge_model
    messages = build_messages(
        system_prompt=VCA_BEHAVIOR_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(input, criteria, vca_context),
    )

    try:
        parsed, _ = await run_grading_judge(
            model=model,
            messages=messages,
            timeout=VCA_BEHAVIOR_TIMEOUT_SECONDS,
            extra_args=input.grading_settings.llm_judge_extra_args,
            task_id=str(input.verifier.task_id or "unknown"),
            max_json_retries=MAX_JSON_RETRIES,
            call_llm_fn=call_llm,
            extra_args_for_attempt=_extra_args_for_attempt,
            log_prefix="[VCA_BEHAVIOR]",
        )
    except ValueError as e:
        message = str(e)
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            message=message,
            verifier_result_values={
                "judge_grade": "error",
                "grade_rationale": message,
                "vca_id": input.trajectory.vca_id,
                "ta_trajectory_id": input.trajectory.ta_trajectory_id,
            },
        )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if parsed.is_criteria_true else 0.0,
        verifier_result_values={
            "judge_grade": "pass" if parsed.is_criteria_true else "fail",
            "grade_rationale": parsed.rationale,
            "vca_id": input.trajectory.vca_id,
            "ta_trajectory_id": input.trajectory.ta_trajectory_id,
        },
    )


def _get_vca_context(input: EvalImplInput) -> VcaContext | None:
    if not input.helper_results:
        return None
    value = input.helper_results.get(HelperIds.VCA_CONTEXT)
    return value if isinstance(value, VcaContext) else None


def _build_user_prompt(
    input: EvalImplInput, criteria: str, vca_context: VcaContext | None
) -> str:
    verifier_values = input.verifier.verifier_values or {}
    payload: dict[str, Any] = {
        "vca_id": input.trajectory.vca_id,
        "ta_trajectory_id": input.trajectory.ta_trajectory_id,
        "trajectory_status": input.trajectory.status,
        "trajectory_messages": input.trajectory.messages,
        "trajectory_output": input.trajectory.output,
        "matching_vca_context": vca_context.model_dump(mode="json")
        if vca_context
        else None,
    }
    criteria_explanation = verifier_values.get("criteria_explanation")
    explanation_section = (
        f"\n<CRITERIA_EXPLANATION>\n{criteria_explanation}\n</CRITERIA_EXPLANATION>"
        if criteria_explanation
        else ""
    )
    evidence = json.dumps(payload, default=str, indent=2)
    return f"""Here is the selected VCA trajectory and coordinator evidence for evaluation:

<VCA_EVIDENCE>
{evidence}
</VCA_EVIDENCE>

<VERIFICATION_CRITERIA>
{criteria}
</VERIFICATION_CRITERIA>{explanation_section}

<REMINDER>
- Evaluate whether the selected VCA behavior meets the VERIFICATION_CRITERIA.
- Ignore target-agent behavior except as context for what the VCA was responding to.
- Use the RATIONALE_FORMAT from system instructions.
- Return JSON with rationale and is_criteria_true.
</REMINDER>"""


def _extra_args_for_attempt(
    extra_args: dict[str, Any] | None, attempt: int
) -> dict[str, Any] | None:
    if attempt == 0:
        return extra_args
    return {
        **(extra_args or {}),
        "reasoning_effort": "low",
        "drop_params": True,
    }
