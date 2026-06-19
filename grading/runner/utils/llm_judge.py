"""Shared utilities for LLM-based judge evaluations.

Provides common patterns for:
- JSON response parsing with retry
- Handling Gemini quirks (dict reasons)
- Structured judge response models
"""

import json
import re
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.utils.llm import call_llm

# Max retries for JSON validation errors (matches output_llm pattern)
MAX_JSON_RETRIES = 10

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

# Matches a JSON payload wrapped in a markdown code fence, e.g.
# ```json\n{...}\n``` or ```\n{...}\n```
_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(?P<body>.*?)\n?\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _first_balanced_json(text: str) -> str | None:
    """Return the first balanced ``{...}`` or ``[...]`` substring that parses
    as JSON, or ``None`` if there isn't one. Quote-aware so braces inside
    strings don't throw off the depth count. Keeps scanning past balanced
    blocks that fail to parse, so harmless balanced braces in a preamble don't
    hide a valid object that appears later in the response."""
    n = len(text)
    i = 0
    while i < n:
        open_ch = text[i]
        if open_ch not in "{[":
            i += 1
            continue
        close_ch = "}" if open_ch == "{" else "]"
        depth = 0
        in_str = False
        escape = False
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    candidate = text[i : j + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        break  # not valid JSON; resume scanning after this opener
        i += 1
    return None


def extract_json_payload(raw_content: str) -> str:
    """Best-effort extraction of a JSON payload from an LLM judge response.

    Models intermittently wrap structured output in a markdown code fence
    (```json ... ```) or prepend a short preamble, which makes
    ``model_validate_json`` fail even though the JSON itself is well formed.
    Left unhandled this burns every ``MAX_JSON_RETRIES`` attempt and raises
    "Invalid JSON after N attempts" (issue #97) -- a grader failure that drops
    or zeroes an otherwise-gradeable run.

    This strips a surrounding code fence and, failing that, extracts the first
    balanced JSON object/array so a valid-but-wrapped response parses on the
    first attempt. The original string is returned unchanged when no cleaner
    payload is found, so existing validation/retry behaviour is preserved.
    """
    if not raw_content:
        return raw_content
    text = raw_content.strip()

    # 1) If the response already parses as JSON, use it verbatim. This has to
    #    come before fence stripping: a valid payload whose string fields
    #    contain ``` segments would otherwise be mangled by the fence regex.
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # 2) Strip a surrounding markdown code fence and use the body if it parses.
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        body = fence.group("body").strip()
        try:
            json.loads(body)
            return body
        except json.JSONDecodeError:
            text = body  # search the unwrapped body for a balanced block below

    # 3) Fall back to the first balanced JSON block (handles preamble text).
    block = _first_balanced_json(text)
    return block if block is not None else text


class JudgeResponse(BaseModel):
    """Base response schema for LLM judge output.

    All judge evaluations return a binary pass/fail result with explanation.
    """

    result: int  # 1 = pass, 0 = fail
    reason: str


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
            logger.warning(
                f"[{log_prefix}] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: "
                f"empty response"
            )
            continue

        raw_content = choices[0].message.content
        if not raw_content:
            logger.warning(
                f"[{log_prefix}] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: "
                f"empty content"
            )
            continue

        try:
            # Strip code fences / preamble so a valid-but-wrapped response
            # parses on the first attempt instead of exhausting retries (#97).
            raw_content = extract_json_payload(raw_content)
            # Handle Gemini quirk where reason may be a dict instead of string
            try:
                raw_json = json.loads(raw_content)
                if isinstance(raw_json, list) and len(raw_json) == 1:
                    raw_json = raw_json[0]
                    raw_content = json.dumps(raw_json)
                    logger.debug(f"[{log_prefix}] Unwrapped list response")
                if isinstance(raw_json, dict) and isinstance(
                    raw_json.get("reason"), dict
                ):
                    raw_json["reason"] = json.dumps(raw_json["reason"])
                    raw_content = json.dumps(raw_json)
                    logger.debug(f"[{log_prefix}] Stringified dict reason")
            except json.JSONDecodeError:
                pass  # Let model_validate_json handle JSON errors

            parsed_response = response_class.model_validate_json(raw_content)
            break
        except ValidationError as e:
            logger.warning(
                f"[{log_prefix}] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
            )
            continue

    if parsed_response is None:
        raise ValueError(f"Invalid JSON after {MAX_JSON_RETRIES} attempts")

    return parsed_response
