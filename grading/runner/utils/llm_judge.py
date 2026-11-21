"""Shared utilities for LLM-based judge evaluations.

Provides common patterns for:
- JSON response parsing with retry
- Handling Gemini quirks (dict reasons)
- Structured judge response models
"""

import json
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.utils.llm import call_llm

# Max retries for JSON validation errors (matches output_llm pattern)
MAX_JSON_RETRIES = 10

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600


class JudgeResponse(BaseModel):
    """Base response schema for LLM judge output.

    All judge evaluations return a binary pass/fail result with explanation.
    """

    result: int  # 1 = pass, 0 = fail
    reason: str


def load_first_json_object(raw_content: str) -> dict[str, Any]:
    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            return value[0]

    decoder = json.JSONDecoder()
    start = 0
    while (start := raw_content.find("{", start)) != -1:
        try:
            value, _ = decoder.raw_decode(raw_content, start)
        except json.JSONDecodeError as err:
            if raw_content[start + 1 :].lstrip().startswith('"'):
                raise ValueError("Malformed JSON object in judge response") from err
            start += 1
            continue
        if isinstance(value, dict):
            return value
        start += 1

    raise ValueError("No JSON object found in judge response")


async def call_llm_judge[T: JudgeResponse](
    model: str,
    messages: list[dict[str, Any]],
    response_class: type[T],
    timeout: int = LLM_JUDGE_TIMEOUT,
    extra_args: dict[str, Any] | None = None,
    log_prefix: str = "LLM_JUDGE",
) -> T:
    """Call LLM judge with JSON response parsing and retry logic.

    Handles common issues like:
    - Empty responses from LLM
    - JSON validation errors
    - Gemini returning dict instead of string for reason field

    Args:
        model: LLM model identifier (e.g., "vertex_ai/gemini-2.5-flash")
        messages: Message list for the LLM
        response_class: Pydantic model class for response validation
        timeout: Request timeout in seconds
        extra_args: Additional LLM arguments
        log_prefix: Prefix for log messages

    Returns:
        Validated response of type T

    Raises:
        ValueError: If valid JSON response not obtained after MAX_JSON_RETRIES
    """
    parsed_response = None
    last_raw_content: str | None = None

    for attempt in range(MAX_JSON_RETRIES):
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=timeout,
            extra_args=extra_args,
            response_format={"type": "json_object"},
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            last_raw_content = None
            logger.warning(
                f"[{log_prefix}] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: "
                f"empty response"
            )
            continue

        raw_content = choices[0].message.content
        last_raw_content = raw_content if isinstance(raw_content, str) else None
        if not raw_content:
            logger.warning(
                f"[{log_prefix}] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: "
                f"empty content"
            )
            continue

        try:
            # Handle Gemini quirk where reason may be a dict instead of string
            try:
                raw_json = load_first_json_object(raw_content)
                if isinstance(raw_json, dict) and isinstance(
                    raw_json.get("reason"), dict
                ):
                    raw_json["reason"] = json.dumps(raw_json["reason"])
                    logger.debug(f"[{log_prefix}] Stringified dict reason")
                raw_content = json.dumps(raw_json)
            except ValueError:
                pass  # Let model_validate_json handle JSON errors

            parsed_response = response_class.model_validate_json(raw_content)
            break
        except ValidationError as e:
            logger.warning(
                f"[{log_prefix}] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
            )
            continue

    if parsed_response is None:
        snippet = last_raw_content[-500:] if last_raw_content else "<empty>"
        raise ValueError(
            f"Invalid JSON after {MAX_JSON_RETRIES} attempts; "
            f"last response: {snippet!r}"
        )

    return parsed_response
