"""
Response Tool Verifier

Evaluates agent response and tool artifacts against criteria using LLM judge.

This verifier is designed for grading outputs from agents that may include:
- Text responses from the model
- Tool execution results (code execution, file outputs, etc.)
- Base64 encoded file artifacts (decoded to text before grading)
"""

from typing import Any

from litellm import Choices
from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult
from runner.utils.llm import build_messages, call_llm

from .helpers import (
    extract_agent_response,
    extract_task_prompt,
    format_tool_artifacts,
)
from .prompts import (
    GRADING_SYSTEM_PROMPT,
    GRADING_USER_PROMPT,
    GRADING_USER_PROMPT_NO_ARTIFACTS,
    GradingResponseSchema,
)

# Default timeout for LLM judge calls
LLM_JUDGE_TIMEOUT = 600


async def response_tool_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """
    Grade agent response and tool artifacts using LLM judge.

    Evaluates the agent's response and tool artifacts against criteria.

    Input (verifier_values):
        - criteria: The criterion to evaluate (required)
        - negative_criteria: Optional negative criterion for partial credit
        - criteria_explanation: Additional context for the criteria

    The output data is expected in trajectory.output:
        - raw_response: Full LLM response
        - tool_artifacts: Extracted tool execution results

    Returns:
        VerifierResult with score:
        - 1.0: Criterion is satisfied
        - 0.0: Criterion is not satisfied
        - -1.0: Criterion not satisfied AND negative criterion violated
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    criteria = verifier_values.get("criteria", "")
    negative_criteria = (verifier_values.get("negative_criteria", "") or "").strip()
    criteria_explanation = verifier_values.get("criteria_explanation", "") or ""

    if not criteria:
        raise ValueError("Missing required field: criteria")

    logger.info(
        f"[RESPONSE_TOOL_VERIFIER] task={task_id} | Evaluating criterion: {criteria[:80]}..."
    )

    model = input.grading_settings.llm_judge_model
    extra_args = input.grading_settings.llm_judge_extra_args

    try:
        task_prompt = extract_task_prompt(input)
        agent_response = extract_agent_response(input)

        trajectory_output = None
        if input.trajectory and input.trajectory.output:
            trajectory_output = input.trajectory.output

        tool_artifacts_formatted, evaluated_artifacts = format_tool_artifacts(
            trajectory_output
        )

        logger.debug(
            f"[RESPONSE_TOOL_VERIFIER] task={task_id} | "
            f"task_prompt_len={len(task_prompt)}, "
            f"response_len={len(agent_response)}, "
            f"artifacts_len={len(tool_artifacts_formatted)}"
        )

        has_meaningful_artifacts = (
            tool_artifacts_formatted != "(No tool artifacts)"
            and tool_artifacts_formatted != "(No tool artifacts found)"
        )

        explanation_section = ""
        if criteria_explanation:
            explanation_section = f"\n### Additional Context\n{criteria_explanation}"

        if has_meaningful_artifacts:
            user_prompt = GRADING_USER_PROMPT.format(
                task_prompt=task_prompt,
                agent_response=agent_response,
                tool_artifacts=tool_artifacts_formatted,
                criteria=criteria,
                criteria_explanation=explanation_section,
            )
        else:
            user_prompt = GRADING_USER_PROMPT_NO_ARTIFACTS.format(
                task_prompt=task_prompt,
                agent_response=agent_response,
                criteria=criteria,
                criteria_explanation=explanation_section,
            )

        messages = build_messages(
            system_prompt=GRADING_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format=GradingResponseSchema,
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")

        raw_content = choices[0].message.content
        if not raw_content:
            raise ValueError("LLM returned empty content")

        parsed = GradingResponseSchema.model_validate_json(raw_content)

        is_criteria_true = parsed.is_criteria_true
        rationale = parsed.rationale

        logger.info(
            f"[RESPONSE_TOOL_VERIFIER] task={task_id} | "
            f"result={'PASS' if is_criteria_true else 'FAIL'} | "
            f"rationale: {rationale[:100]}..."
        )

        result_values: dict[str, Any] = {
            "judge_grade": "pass" if is_criteria_true else "fail",
            "grade_rationale": rationale,
            "evaluated_artifacts": evaluated_artifacts,
        }

        if is_criteria_true:
            score = 1.0
        elif negative_criteria:
            score = await _evaluate_negative_criteria(
                input=input,
                task_prompt=task_prompt,
                agent_response=agent_response,
                negative_criteria=negative_criteria,
                tool_artifacts_formatted=tool_artifacts_formatted,
                has_meaningful_artifacts=has_meaningful_artifacts,
                model=model,
                extra_args=extra_args,
                result_values=result_values,
            )
        else:
            score = 0.0

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values=result_values,
        )

    except Exception as e:
        error_msg = f"Response tool verification failed: {e}"
        logger.error(f"[RESPONSE_TOOL_VERIFIER] task={task_id} | {error_msg}")
        raise ValueError(error_msg) from e


async def _evaluate_negative_criteria(
    input: EvalImplInput,
    task_prompt: str,
    agent_response: str,
    negative_criteria: str,
    tool_artifacts_formatted: str,
    has_meaningful_artifacts: bool,
    model: str,
    extra_args: dict[str, Any] | None,
    result_values: dict[str, Any],
) -> float:
    """
    Evaluate negative criteria for partial credit scoring.

    Called when the main criterion fails. Checks if the agent also violated
    a negative criterion, which results in a penalty score (-1.0).

    Args:
        input: The eval input containing verifier info
        task_prompt: The extracted task prompt
        agent_response: The agent's response text
        negative_criteria: The negative criterion to check
        tool_artifacts_formatted: Formatted tool artifacts string
        has_meaningful_artifacts: Whether there are artifacts to include
        model: LLM model to use for judging
        extra_args: Additional LLM arguments
        result_values: Dict to update with negative grade results

    Returns:
        -1.0 if negative criterion violated, 0.0 otherwise

    Raises:
        ValueError: If LLM returns empty response for negative criterion
    """
    task_id = input.verifier.task_id or "unknown"

    if has_meaningful_artifacts:
        neg_user_prompt = GRADING_USER_PROMPT.format(
            task_prompt=task_prompt,
            agent_response=agent_response,
            tool_artifacts=tool_artifacts_formatted,
            criteria=negative_criteria,
            criteria_explanation="",
        )
    else:
        neg_user_prompt = GRADING_USER_PROMPT_NO_ARTIFACTS.format(
            task_prompt=task_prompt,
            agent_response=agent_response,
            criteria=negative_criteria,
            criteria_explanation="",
        )

    neg_messages = build_messages(
        system_prompt=GRADING_SYSTEM_PROMPT,
        user_prompt=neg_user_prompt,
    )

    neg_response = await call_llm(
        model=model,
        messages=neg_messages,
        timeout=LLM_JUDGE_TIMEOUT,
        extra_args=extra_args,
        response_format=GradingResponseSchema,
    )

    neg_choices = neg_response.choices
    if not neg_choices or not isinstance(neg_choices[0], Choices):
        raise ValueError("LLM returned empty response for negative criterion")

    neg_raw_content = neg_choices[0].message.content
    if not neg_raw_content:
        raise ValueError("LLM returned empty content for negative criterion")

    neg_parsed = GradingResponseSchema.model_validate_json(neg_raw_content)
    violated_negative = neg_parsed.is_criteria_true
    negative_rationale = neg_parsed.rationale

    result_values["negative_grade"] = "pass" if violated_negative else "fail"
    result_values["negative_grade_rationale"] = negative_rationale

    logger.debug(
        f"[RESPONSE_TOOL_VERIFIER] task={task_id} | "
        f"negative_check={'VIOLATED' if violated_negative else 'OK'}"
    )

    return -1.0 if violated_negative else 0.0
