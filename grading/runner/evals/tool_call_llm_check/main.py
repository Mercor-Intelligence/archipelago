"""Tool Call LLM Check - LLM-based evaluation of tool calls against custom criteria.

This verifier uses an LLM judge to evaluate whether the agent's tool usage
satisfies custom criteria. Unlike the deterministic TOOL_CALL_CHECK which
checks if specific tools were called, this verifier can evaluate nuanced
behaviors like "Did the agent successfully read the data from the spreadsheet?"
rather than just "Did the agent call excel_read()".

Example criteria:
- "The agent successfully sent an email to Steve"
- "The agent read the contents of budget.xlsx and used the data in its response"
- "The agent created a new file with the correct content"
"""

from collections.abc import Sequence
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, Field

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages, call_llm
from runner.utils.trajectory import (
    extract_tool_calls_with_outputs,
    format_tool_calls_for_prompt,
)

# Default timeout for LLM judge calls (5 minutes)
LLM_JUDGE_TIMEOUT = 300


class CriteriaEvaluationResponse(BaseModel):
    """Response from LLM evaluating whether criteria are satisfied."""

    satisfied: bool = Field(
        description="Whether the criteria are satisfied based on the trajectory"
    )
    reasoning: str = Field(
        description="Detailed explanation of why the criteria are or are not satisfied"
    )
    relevant_evidence: list[str] = Field(
        default_factory=list,
        description="Key pieces of evidence from the trajectory supporting the judgment",
    )


def _build_evaluation_prompt(
    criteria: str,
    task_prompt: str,
    tool_calls_formatted: str,
    messages_formatted: str | None = None,
) -> str:
    """Build the prompt for LLM to evaluate the criteria.

    Args:
        criteria: The criteria to evaluate
        task_prompt: The original task given to the agent
        tool_calls_formatted: Formatted string of tool calls and outputs
        messages_formatted: Optional formatted conversation history
    """
    prompt = f"""You are evaluating whether an AI agent's actions satisfy specific criteria.

## Criteria to Evaluate
{criteria}

## Original Task
{task_prompt}

## Agent's Tool Calls and Results
{tool_calls_formatted}
"""

    if messages_formatted:
        prompt += f"""
## Conversation History
{messages_formatted}
"""

    prompt += """
## Your Task
Evaluate whether the agent's actions (as shown in the tool calls and their outputs) satisfy the criteria.

Consider:
1. Did the agent attempt the action described in the criteria?
2. Did the tool calls succeed in accomplishing what the criteria describe?
3. Is there evidence in the tool outputs that confirms the criteria are satisfied?

Be precise in your evaluation:
- If the criteria say "sent an email to Steve", verify the email was actually sent (check tool output)
- If the criteria say "read from X file", verify the file was read and data was obtained
- If the criteria say "created a file with Y content", verify the file creation succeeded

Return your evaluation as JSON with:
- satisfied: true if the criteria are satisfied, false otherwise
- reasoning: Detailed explanation of your judgment
- relevant_evidence: List of specific pieces of evidence from the tool outputs
"""

    return prompt


def _format_messages_for_prompt(
    messages: Sequence[Any],
    max_messages: int = 20,
) -> str:
    """Format conversation messages for inclusion in the prompt.

    Args:
        messages: List of message dicts with role and content
        max_messages: Maximum number of messages to include
    """
    formatted_parts = []
    message_count = 0

    for idx, msg in enumerate(messages):
        if message_count >= max_messages:
            remaining = len(messages) - idx
            if remaining > 0:
                formatted_parts.append(f"... [{remaining} more messages]")
            break

        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        # Skip tool messages (they're covered in tool_calls_formatted)
        if role == "tool":
            continue

        # Handle multimodal content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            content = "\n".join(text_parts)

        if content:
            # Truncate very long messages
            if len(content) > 1000:
                content = content[:1000] + "... [TRUNCATED]"
            formatted_parts.append(f"**{role.upper()}:** {content}")
            message_count += 1

    return "\n\n".join(formatted_parts)


def _extract_task_prompt(messages: Sequence[Any]) -> str:
    """Task prompt = the first user message with usable text.

    Multi-part content joins ALL text blocks, and pydantic validates
    Iterable[...] message content lazily (a ValidatorIterator, not a list) —
    iterate it, never isinstance-check list or str() it (that would stringify
    the iterator repr into the prompt).
    """
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content:
                return content
            continue
        if content is None:
            continue
        texts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        if texts:
            return "\n".join(texts)
    return ""


async def tool_call_llm_check_eval(input: EvalImplInput) -> VerifierResult:
    """Evaluate tool calls against custom criteria using LLM.

    This verifier uses an LLM judge to evaluate whether the agent's tool usage
    satisfies custom criteria specified in verifier_values.

    Config fields (verifier_values):
        criteria (required): The criteria to evaluate, e.g.:
            - "The agent successfully sent an email to Steve"
            - "The agent read the contents of budget.xlsx"
        include_tool_outputs (optional, default True): Include tool outputs in context
        include_messages (optional, default False): Include full conversation history

    Scoring:
        - 1.0 if criteria are satisfied
        - 0.0 if criteria are not satisfied

    Returns:
        VerifierResult with score and detailed reasoning
    """
    # Get configuration
    verifier_values = input.verifier.verifier_values or {}
    # Backward-compat: accept legacy "criterion" key for verifiers authored before
    # the rename to "criteria" (see PR renaming TOOL_CALL_LLM_CHECK fields for
    # consistency with other verifiers). New writes use "criteria".
    criteria = verifier_values.get("criteria") or verifier_values.get("criterion", "")
    include_tool_outputs = verifier_values.get("include_tool_outputs", True)
    include_messages = verifier_values.get("include_messages", False)

    # Validate criteria are provided
    if not criteria or not criteria.strip():
        logger.error("No criteria provided in verifier_values")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "error": "No criteria provided. Please specify criteria to evaluate.",
                "satisfied": False,
                "reasoning": "Configuration error: criteria are required",
            },
        )

    # Get LLM configuration
    model = input.grading_settings.llm_judge_model
    extra_args = input.grading_settings.llm_judge_extra_args

    logger.info(f"Starting tool call LLM check with model: {model}")
    logger.info(f"Criteria: {criteria}")

    # Extract task prompt (first user message)
    task_prompt = _extract_task_prompt(input.trajectory.messages)

    if not task_prompt:
        logger.warning("No task prompt found in trajectory")
        task_prompt = "[No task prompt found]"

    # Extract tool calls with outputs
    tool_calls = extract_tool_calls_with_outputs(input.trajectory.messages)

    if not tool_calls:
        logger.info("No tool calls found in trajectory")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.OK,
            verifier_result_values={
                "satisfied": False,
                "reasoning": "No tool calls found in trajectory. Cannot evaluate criteria.",
                "relevant_evidence": [],
                "criteria": criteria,
                "judge_grade": "fail",
                "tool_calls_evaluated": 0,
            },
        )

    logger.info(f"Found {len(tool_calls)} tool calls to evaluate")

    # Format tool calls for prompt
    tool_calls_formatted = format_tool_calls_for_prompt(
        tool_calls, include_outputs=include_tool_outputs, max_output_length=2000
    )

    # Optionally format messages
    messages_formatted = None
    if include_messages:
        messages_formatted = _format_messages_for_prompt(input.trajectory.messages)

    # Build evaluation prompt
    evaluation_prompt = _build_evaluation_prompt(
        criteria=criteria,
        task_prompt=task_prompt,
        tool_calls_formatted=tool_calls_formatted,
        messages_formatted=messages_formatted,
    )

    # Call LLM to evaluate
    messages = build_messages(
        system_prompt=(
            "You are an expert evaluator assessing whether an AI agent's actions "
            "satisfy specific criteria. Evaluate based on the evidence in the "
            "tool calls and their outputs. Be precise and evidence-based in your judgment."
        ),
        user_prompt=evaluation_prompt,
    )

    try:
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format=CriteriaEvaluationResponse,
        )

        # Parse response
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")

        raw_content = choices[0].message.content
        if not raw_content:
            raise ValueError("LLM returned empty content")

        evaluation = CriteriaEvaluationResponse.model_validate_json(raw_content)

        score = 1.0 if evaluation.satisfied else 0.0
        judge_grade = "pass" if evaluation.satisfied else "fail"

        logger.info(f"Criteria evaluation complete: {judge_grade}")
        logger.info(f"Reasoning: {evaluation.reasoning[:200]}...")

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            status=VerifierResultStatus.OK,
            verifier_result_values={
                "satisfied": evaluation.satisfied,
                "reasoning": evaluation.reasoning,
                "relevant_evidence": evaluation.relevant_evidence,
                "criteria": criteria,
                "judge_grade": judge_grade,
                "tool_calls_evaluated": len(tool_calls),
            },
        )

    except Exception as e:
        error_msg = f"Tool call LLM evaluation failed: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg) from e
