import json
from collections.abc import Awaitable, Callable
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import ValidationError

from runner.evals.output_llm.utils.prompts import GradingResponseSchema
from runner.evals.output_llm.utils.shared import MAX_JSON_RETRIES
from runner.utils.llm import call_llm as default_call_llm

CallLlm = Callable[..., Awaitable[Any]]
ExtraArgsForAttempt = Callable[[dict[str, Any] | None, int], dict[str, Any] | None]


async def run_grading_judge(
    *,
    model: str,
    messages: list[dict[str, Any]],
    timeout: int,
    extra_args: dict[str, Any] | None,
    task_id: str,
    max_json_retries: int = MAX_JSON_RETRIES,
    call_llm_fn: CallLlm = default_call_llm,
    extra_args_for_attempt: ExtraArgsForAttempt | None = None,
    log_prefix: str = "[JUDGE]",
) -> tuple[GradingResponseSchema, str | None]:
    parsed: GradingResponseSchema | None = None
    raw_content: str | None = None

    for attempt in range(max_json_retries):
        response = await call_llm_fn(
            model=model,
            messages=messages,
            timeout=timeout,
            extra_args=extra_args_for_attempt(extra_args, attempt)
            if extra_args_for_attempt
            else extra_args,
            response_format=GradingResponseSchema,
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            logger.warning(
                f"{log_prefix} task={task_id} JSON retry "
                f"{attempt + 1}/{max_json_retries}: empty response"
            )
            continue

        raw_content = choices[0].message.content
        if not raw_content:
            logger.warning(
                f"{log_prefix} task={task_id} JSON retry "
                f"{attempt + 1}/{max_json_retries}: empty content"
            )
            continue

        try:
            parsed = parse_grading_response(raw_content)
            break
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            logger.warning(
                f"{log_prefix} task={task_id} JSON retry "
                f"{attempt + 1}/{max_json_retries}: "
                f"finish_reason={getattr(choices[0], 'finish_reason', None)} error={e}"
            )
            continue

    if parsed is None:
        raise ValueError(f"Invalid JSON after {max_json_retries} attempts")

    return parsed, raw_content


def parse_grading_response(raw_content: str) -> GradingResponseSchema:
    raw_json = _load_first_json_object(raw_content)
    if isinstance(raw_json.get("rationale"), dict):
        raw_json["rationale"] = json.dumps(raw_json["rationale"])
    return GradingResponseSchema.model_validate(raw_json)


def _load_first_json_object(raw_content: str) -> dict[str, Any]:
    try:
        value = json.loads(raw_content)
    except json.JSONDecodeError:
        start = raw_content.find("{")
        if start < 0:
            raise
        value, _ = json.JSONDecoder().raw_decode(raw_content[start:])
    if not isinstance(value, dict):
        raise ValueError("Judge response must be a JSON object")
    return value
