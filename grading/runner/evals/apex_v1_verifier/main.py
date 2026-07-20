"""Apex V1 Verifier - evaluates model response against a specific criterion using LLM"""

import json
import re
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.llm import build_messages, call_llm

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

# Maximum number of retries for JSON parsing
MAX_JSON_RETRIES = 10

INVALID_ESCAPE_RE = re.compile(r"\\(?![\"\\/bfnrtu])")
PARTIAL_UNICODE_ESCAPE_RE = re.compile(r"\\u(?![0-9a-fA-F]{4})")


class CriterionJudgeResponse(BaseModel):
    """Response schema for criterion judge LLM output."""

    result: int  # 1 = pass, 0 = fail
    reason: str  # Explanation for the judgment


# System prompt for criterion evaluation
CRITERION_JUDGE_SYSTEM_PROMPT = """You are evaluating a model-generated response against a specific criterion. Your task is to determine if the response satisfies this criterion and provide a concise explanation."""


def _build_criterion_judge_prompt(
    criterion_description: str,
    solution: str,
) -> str:
    """Build the user prompt for criterion evaluation."""
    return f"""Criterion to evaluate: {criterion_description}

Response to evaluate: {solution}

Instructions:

1. First, analyze the response against the given criterion.

2. Determine if the response fully satisfies the criterion (result = 1) or not (result = 0).

3. Provide a concise explanation (maximum 2-3 sentences) that:

    a. States whether the criterion is met or not

    b. Points to specific evidence from the response

    c. Avoids unnecessary details or repetition

Return your evaluation in the following JSON format:

{{
    "result": <1 or 0>,
    "reason": "<concise explanation>"
}}

Keep your explanation brief and focus on the key points that justify your result."""


def _fix_unescaped_backslashes(text: str) -> str:
    """
    Fix unescaped backslashes in text.
    """
    if "\\" not in text:
        return text

    fixed = INVALID_ESCAPE_RE.sub(r"\\\\", text)
    fixed = PARTIAL_UNICODE_ESCAPE_RE.sub(r"\\\\u", fixed)
    return fixed


def _parse_json(candidate: str) -> dict[str, Any] | None:
    """Attempt to parse JSON, retrying once with repaired backslashes."""
    candidate = candidate.strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    try:
        return json.loads(_fix_unescaped_backslashes(candidate))
    except json.JSONDecodeError:
        return None


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """
    Extract JSON object from text that may contain additional content.

    Handles cases where the LLM returns JSON wrapped in markdown code blocks
    or with additional text before/after.

    Args:
        text: Raw text that may contain JSON

    Returns:
        Parsed JSON dict if found, None otherwise
    """
    if not text:
        return None

    # First, try direct JSON parse
    parsed = _parse_json(text)
    if parsed is not None:
        return parsed

    # Try to extract JSON from markdown code blocks
    code_block_patterns = [
        r"```json\s*([\s\S]*?)\s*```",
        r"```\s*([\s\S]*?)\s*```",
    ]

    for pattern in code_block_patterns:
        matches = re.findall(pattern, text, re.MULTILINE)
        for match in matches:
            parsed = _parse_json(match)
            if parsed is not None:
                return parsed

    # Try to find JSON object pattern in the text
    json_pattern = r"\{[^{}]*\"result\"[^{}]*\"reason\"[^{}]*\}"
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        parsed = _parse_json(match)
        if parsed is not None:
            return parsed

    # Try finding any JSON object in the text
    brace_pattern = r"\{[\s\S]*?\}"
    matches = re.findall(brace_pattern, text)
    for match in matches:
        parsed = _parse_json(match)
        if parsed and "result" in parsed and "reason" in parsed:
            return parsed

    return None


def _validate_response(parsed_json: dict[str, Any]) -> CriterionJudgeResponse:
    """
    Validate and convert parsed JSON to CriterionJudgeResponse.

    Args:
        parsed_json: Parsed JSON dictionary

    Returns:
        Validated CriterionJudgeResponse

    Raises:
        ValidationError: If validation fails
    """
    # Normalize result field - accept bool or int
    result = parsed_json.get("result")
    if isinstance(result, bool):
        parsed_json["result"] = 1 if result else 0
    elif isinstance(result, str):
        # Handle string representations
        lower_result = result.lower().strip()
        if lower_result in ("1", "true", "pass", "yes"):
            parsed_json["result"] = 1
        elif lower_result in ("0", "false", "fail", "no"):
            parsed_json["result"] = 0

    return CriterionJudgeResponse.model_validate(parsed_json)


async def _call_llm_with_retry(
    model: str,
    messages: list[dict[str, Any]],
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> CriterionJudgeResponse:
    """
    Call LLM and retry until valid JSON is returned.

    Args:
        model: LLM model to use
        messages: Message list for the LLM
        extra_args: Extra LLM arguments
        task_id: Task ID for logging

    Returns:
        Validated CriterionJudgeResponse

    Raises:
        ValueError: If unable to get valid JSON after max retries
    """
    last_error: Exception | None = None

    for attempt in range(MAX_JSON_RETRIES):
        logger.info(
            f"[APEX_V1] task={task_id} | attempt={attempt + 1}/{MAX_JSON_RETRIES}"
        )

        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            last_error = ValueError("LLM returned empty response")
            logger.warning(
                f"[APEX_V1] task={task_id} | empty response on attempt {attempt + 1}"
            )
            continue

        raw_content = choices[0].message.content
        if not raw_content:
            last_error = ValueError("LLM returned empty content")
            logger.warning(
                f"[APEX_V1] task={task_id} | empty content on attempt {attempt + 1}"
            )
            continue

        logger.debug(f"[APEX_V1] task={task_id} | raw response:\n{raw_content}")

        # Try to extract and validate JSON
        try:
            parsed_json = _extract_json_from_text(raw_content)
            if parsed_json is None:
                last_error = ValueError(
                    f"Could not extract JSON from response: {raw_content[:200]}"
                )
                logger.warning(
                    f"[APEX_V1] task={task_id} | "
                    f"JSON extraction failed on attempt {attempt + 1}"
                )
                continue

            validated = _validate_response(parsed_json)
            logger.info(
                f"[APEX_V1] task={task_id} | "
                f"successfully parsed on attempt {attempt + 1}"
            )
            return validated

        except ValidationError as e:
            last_error = e
            logger.warning(
                f"[APEX_V1] task={task_id} | "
                f"validation failed on attempt {attempt + 1}: {e}"
            )
            continue

        except json.JSONDecodeError as e:
            last_error = e
            logger.warning(
                f"[APEX_V1] task={task_id} | "
                f"JSON decode failed on attempt {attempt + 1}: {e}"
            )
            continue

    # All retries exhausted
    raise ValueError(
        f"Failed to get valid JSON response after {MAX_JSON_RETRIES} attempts. "
        f"Last error: {last_error}"
    )


async def apex_v1_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """
    Apex V1 Verifier - Evaluate model response against a specific criterion using LLM judge.

    This verifier supports rich metadata fields:
    - description: The criterion description to evaluate against
    - sources: Source documents for the criterion
    - justification: Expected justification/evidence
    - weight: Criterion priority (e.g., "Primary objective(s)")
    - human_rating: Whether human rating is available
    - criterion_type: Type of criterion (e.g., ["Extraction (recall)"])
    - dependent_criteria: List of criteria this depends on

    If the LLM fails to return valid JSON, it retries up to MAX_JSON_RETRIES times.

    Returns binary score: 1.0 (pass) or 0.0 (fail).
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    # 1. Get description (required)
    criterion_description = verifier_values.get("description", "")
    if not criterion_description:
        raise ValueError("Missing required field: description")

    logger.info(
        f"[APEX_V1] task={task_id} | evaluating: {criterion_description[:100]}..."
    )

    try:
        # 2. Get solution from helper data (final answer from agent)
        if not input.helper_results:
            raise ValueError("Missing helper results")

        solution = input.helper_results.get(HelperIds.FINAL_ANSWER)
        if not solution:
            logger.warning(
                f"[APEX_V1] task={task_id} | no solution found, failing criterion"
            )
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                verifier_result_values={
                    "result": 0,
                    "reason": "No model response/solution was found to evaluate.",
                },
            )

        # 3. Get model settings
        model = input.grading_settings.llm_judge_model
        extra_args = input.grading_settings.llm_judge_extra_args

        # 4. Build prompt
        user_prompt = _build_criterion_judge_prompt(
            criterion_description=criterion_description,
            solution=solution,
        )

        # 5. Build messages
        messages = build_messages(
            system_prompt=CRITERION_JUDGE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        logger.debug(
            f"[APEX_V1] task={task_id} | prompt:\n"
            f"SYSTEM:\n{CRITERION_JUDGE_SYSTEM_PROMPT}\n\n"
            f"USER:\n{user_prompt}"
        )

        # 6. Call LLM with JSON retry logic
        parsed_response = await _call_llm_with_retry(
            model=model,
            messages=messages,
            extra_args=extra_args,
            task_id=task_id,
        )

        # 7. Build result
        passed = parsed_response.result == 1
        score = 1.0 if passed else 0.0

        logger.info(
            f"[APEX_V1] task={task_id} | "
            f"result: {'PASS' if passed else 'FAIL'} | "
            f"criterion: {criterion_description[:50]}..."
        )

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "result": parsed_response.result,
                "reason": parsed_response.reason,
            },
        )

    except Exception as e:
        error_msg = f"Apex V1 verifier evaluation failed: {str(e)}"
        logger.error(f"[APEX_V1] task={task_id} | error: {error_msg}")
        raise ValueError(error_msg) from e
