"""Tool Call Check eval - uses LLM to evaluate correctness of tool calls in agent trajectory."""

from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, Field

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages, call_llm
from runner.utils.trajectory import extract_tool_calls_with_outputs

# Default timeout for LLM judge calls (5 minutes)
LLM_JUDGE_TIMEOUT = 300

# Default evaluation prompt template
DEFAULT_EVALUATION_PROMPT = """You are evaluating whether tool calls made by an AI agent executed successfully or failed.

## Task Context
The agent was given the following task:

{task_prompt}

## Tool Calls and Their Outputs
Below are all the tool calls made by the agent, along with the actual responses from the tools:

**Note on Truncation:** When arguments or outputs are very long, they will be truncated while including here :
- Arguments: Truncated at the end with "... [TRUNCATED]"
- Outputs: Truncated in the middle, showing the first and last portions with "... [TRUNCATED] ..." in between

{tool_calls_formatted}

## How to Determine SUCCESS vs FAILURE

Judge ONLY by examining the **tool's response/output content**:

1. **FAILURE** = The output contains error messages, exceptions, or failure indicators
2. **SUCCESS** = The output contains actual data/results without errors that the agent intended to get

### Important Rules:
1. DO NOT look for generic "success" or "tool call succeeded" text - that just means the agent made a call, not that the tool worked
2. ONLY judge based on the actual tool response content shown in the Output section
3. If the output shows an error message or exception, it is a FAILURE regardless of anything else
4. If the output shows real data/results without errors, it is a SUCCESS

### Do NOT evaluate:
- Whether the tool call was necessary or optimal for the task
- Whether it was at the right step in the workflow
- Whether the arguments were strategically appropriate

## Your Task
For each tool call, provide:
- call_number: The tool call number (1, 2, 3, ...)
- tool_name: The tool name
- arguments: Brief summary of arguments (max 100 chars)
- output_summary: Brief summary of the output (max 200 chars)
- rationale: Explain why this is SUCCESS or FAILURE based on what the output contains
- decision: Either "pass" (tool returned valid data) or "fail" (tool returned an error)

Also provide:
- summary: Brief summary (e.g., "18/20 tool calls executed successfully")"""


class ToolCallEvaluation(BaseModel):
    """Evaluation result for a single tool call."""

    call_number: int = Field(description="The tool call number (1, 2, 3, ...)")
    tool_name: str = Field(description="The name of the tool that was called")
    arguments: str = Field(description="Brief summary of arguments (max 100 chars)")
    output_summary: str = Field(
        description="Brief summary of the output (max 200 chars)"
    )
    rationale: str = Field(
        description="Explanation of why this is a success or failure based on the output"
    )
    decision: str = Field(
        description="Either 'pass' (executed successfully) or 'fail' (returned an error)"
    )


class ToolCallEvaluationResponse(BaseModel):
    """Response containing all tool call evaluations."""

    evaluations: list[ToolCallEvaluation] = Field(
        description="List of evaluations for each tool call in the trajectory"
    )
    overall_decision: str = Field(
        description="Overall decision: 'pass' if all tool calls are correct, 'fail' otherwise"
    )
    summary: str = Field(description="Brief summary of the overall evaluation")


def _build_llm_evaluation_prompt(
    task_prompt: str,
    tool_calls: list[dict[str, Any]],
    custom_prompt_template: str | None = None,
) -> str:
    """Build the prompt for LLM to evaluate tool calls.

    Args:
        task_prompt: The original task given to the agent
        tool_calls: List of tool calls with step, name, arguments, and output
        custom_prompt_template: Optional custom prompt template. Can use:
            - {task_prompt}: The task description
            - {tool_calls_formatted}: Formatted list of tool calls with outputs
    """

    # Format tool calls with their outputs for the prompt
    tool_calls_text = []
    for tc in tool_calls:
        args = tc["arguments"]
        if len(args) > 500:
            args = args[:500] + "... [TRUNCATED]"

        output = tc.get("output") or "(no output captured)"

        tool_calls_text.append(
            f"### Tool Call {tc['call_number']}: {tc['tool_name']}\n"
            f"**Arguments:**\n```\n{args}\n```\n"
            f"**Output:**\n```\n{output}\n```\n"
        )

    tool_calls_formatted = "\n".join(tool_calls_text)

    prompt_template = custom_prompt_template or DEFAULT_EVALUATION_PROMPT
    return prompt_template.format(
        task_prompt=task_prompt,
        tool_calls_formatted=tool_calls_formatted,
    )


async def posttraining_tool_call_check_eval(input: EvalImplInput) -> VerifierResult:
    """
    Evaluate tool calls in the agent trajectory using LLM.

    This verifier uses LLM to evaluate each tool call's correctness
    and appropriateness for the given task.

    For each tool call, the LLM provides:
    - step: Which step in the trajectory
    - tool_name: Name of the tool
    - arguments: Arguments passed
    - rationale: Detailed explanation
    - decision: "pass" or "fail"

    Scoring:
    - Score = (passed_calls / total_calls)
    - 1.0 if all tool calls are correct
    - 0.0 if no tool calls found (agent should have made tool calls)
    - 0.0 if no tool calls pass

    Returns:
        VerifierResult with score 0.0 and overall_decision "fail" if no tool calls found.
    """
    # Get LLM configuration from grading settings (standard infrastructure)
    model = input.grading_settings.llm_judge_model
    extra_args = input.grading_settings.llm_judge_extra_args
    # Optional custom prompt template from verifier values (may be None)
    verifier_values = input.verifier.verifier_values or {}
    custom_prompt_template = verifier_values.get("prompt_template")

    logger.info(f"Starting tool call LLM evaluation with model: {model}")
    if custom_prompt_template:
        logger.info("Using custom prompt template")

    # Extract task prompt (first user message)
    task_prompt = ""
    for message in input.trajectory.messages:
        if message.get("role") == "user":
            content = message.get("content")
            task_prompt = str(content) if content else ""
            break

    if not task_prompt:
        logger.warning("No task prompt found in trajectory")
        task_prompt = "[No task prompt found]"

    # Extract tool calls with their outputs
    tool_calls_with_outputs = extract_tool_calls_with_outputs(input.trajectory.messages)

    if not tool_calls_with_outputs:
        logger.info("No tool calls found in trajectory - returning fail")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.OK,
            verifier_result_values={
                "evaluations": [],
                "judge_grade": "fail",
                "grade_rationale": "No tool calls found in trajectory",
            },
        )

    logger.info(f"Found {len(tool_calls_with_outputs)} tool calls to evaluate")

    # Build evaluation prompt (with optional custom template)
    evaluation_prompt = _build_llm_evaluation_prompt(
        task_prompt, tool_calls_with_outputs, custom_prompt_template
    )

    # Call LLM to evaluate
    messages = build_messages(
        system_prompt="You are an expert at evaluating AI agent tool calls for correctness and appropriateness.",
        user_prompt=evaluation_prompt,
    )

    try:
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format=ToolCallEvaluationResponse,
        )

        # Parse and validate response
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")

        raw_content = choices[0].message.content
        if not raw_content:
            raise ValueError("LLM returned empty content")

        response_text = raw_content
        evaluation_result = ToolCallEvaluationResponse.model_validate_json(
            response_text
        )

        total_tool_calls = len(tool_calls_with_outputs)

        # Count passes from LLM's individual evaluations
        llm_passed = sum(
            1
            for eval in evaluation_result.evaluations
            if eval.decision.lower() == "pass"
        )
        passed_calls = min(llm_passed, total_tool_calls)
        score = passed_calls / total_tool_calls if total_tool_calls > 0 else 0.0

        # Overall decision is pass if all tool calls passed, otherwise fail if any tool call failed or no tool calls were made
        overall_decision = "pass" if score == 1.0 else "fail"

        # Build result
        evaluations_list = [
            {
                "call_number": eval.call_number,
                "tool_name": eval.tool_name,
                "arguments": eval.arguments,
                "output_summary": eval.output_summary,
                "rationale": eval.rationale,
                "decision": eval.decision,
            }
            for eval in evaluation_result.evaluations
        ]

        logger.info(
            f"Evaluation complete: {passed_calls}/{total_tool_calls} tool calls passed"
        )

        grade_rationale = (
            f"{passed_calls}/{total_tool_calls} tool calls passed. "
            f"{evaluation_result.summary}"
        )
        result_values = {
            "evaluations": evaluations_list,
            "judge_grade": overall_decision,
            "grade_rationale": grade_rationale,
        }

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values=result_values,
        )

    except Exception as e:
        error_msg = f"Tool call evaluation failed: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg) from e
