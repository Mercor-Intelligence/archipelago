import json
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.vca_context import VcaContext
from runner.models import EvaluationTarget, VerifierResult
from runner.utils.llm import build_messages, call_llm

VCA_BEHAVIOR_SYSTEM_PROMPT = """You are grading a Virtual Coworker Agent (VCA), not the Target Agent.

The VCA is a simulated coworker inside an Archipelago environment. Grade whether the VCA behavior satisfies the criterion. Use only the supplied trajectory, prompt, event, and coordinator evidence.

Return JSON with:
- is_criteria_true: boolean
- rationale: string
"""

VCA_BEHAVIOR_TIMEOUT_SECONDS = 90
MAX_JSON_RETRIES = 3


class VcaBehaviorResponse(BaseModel):
    is_criteria_true: bool
    rationale: str


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

    parsed = None
    raw_content = None
    for attempt in range(MAX_JSON_RETRIES):
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=VCA_BEHAVIOR_TIMEOUT_SECONDS,
            extra_args=input.grading_settings.llm_judge_extra_args,
            response_format={"type": "json_object"},
        )
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            logger.warning(
                f"[VCA_BEHAVIOR] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: empty response"
            )
            continue
        raw_content = choices[0].message.content
        if not raw_content:
            logger.warning(
                f"[VCA_BEHAVIOR] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: empty content"
            )
            continue
        try:
            parsed = VcaBehaviorResponse.model_validate_json(raw_content)
            break
        except ValidationError as e:
            logger.warning(
                f"[VCA_BEHAVIOR] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
            )

    if parsed is None:
        raise ValueError(f"Invalid JSON after {MAX_JSON_RETRIES} attempts")

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
        "criteria": criteria,
        "criteria_explanation": verifier_values.get("criteria_explanation"),
        "vca_id": input.trajectory.vca_id,
        "ta_trajectory_id": input.trajectory.ta_trajectory_id,
        "trajectory_status": input.trajectory.status,
        "trajectory_messages": input.trajectory.messages,
        "trajectory_output": input.trajectory.output,
        "matching_vca_context": vca_context.model_dump(mode="json")
        if vca_context
        else None,
    }
    return json.dumps(payload, default=str, indent=2)
