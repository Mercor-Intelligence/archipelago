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
_MAX_FAILURE_CHARS = 500

BRIEF_RETRY_NUDGE = (
    "Your previous reply ended before the is_criteria_true verdict. "
    "Keep the rationale under 150 words, then give is_criteria_true."
)


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
    finish_reason: str | None = None
    last_error = "no attempts"
    brief = False

    for attempt in range(max_json_retries):
        response = await call_llm_fn(
            model=model,
            messages=_brief_messages(messages) if brief else messages,
            timeout=timeout,
            extra_args=extra_args_for_attempt(extra_args, attempt)
            if extra_args_for_attempt
            else extra_args,
            response_format=GradingResponseSchema,
        )

        choices = response.choices
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
        if not choices or not isinstance(choices[0], Choices):
            last_error = "empty response"
            brief = brief or finish_reason == "length"
            logger.warning(
                f"{log_prefix} task={task_id} JSON retry "
                f"{attempt + 1}/{max_json_retries}: {last_error}"
            )
            continue

        raw_content = choices[0].message.content
        if not raw_content:
            last_error = "empty content"
            brief = brief or finish_reason == "length"
            logger.warning(
                f"{log_prefix} task={task_id} JSON retry "
                f"{attempt + 1}/{max_json_retries}: "
                f"finish_reason={finish_reason} error={last_error}"
            )
            continue

        try:
            parsed = parse_grading_response(raw_content)
            break
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            last_error = str(e)
            brief = brief or finish_reason == "length" or _missing_verdict(e)
            logger.warning(
                f"{log_prefix} task={task_id} JSON retry "
                f"{attempt + 1}/{max_json_retries}: "
                f"finish_reason={finish_reason} error={last_error}"
            )
            continue

    if parsed is None:
        raise ValueError(
            f"Invalid JSON after {max_json_retries} attempts "
            f"(model={model}, last finish_reason={finish_reason}); "
            f"last error: {last_error[:_MAX_FAILURE_CHARS]}"
        )

    return parsed, raw_content


def _missing_verdict(error: Exception) -> bool:
    return isinstance(error, ValidationError) and any(
        err["type"] == "missing" and err["loc"] == ("is_criteria_true",)
        for err in error.errors()
    )


def _brief_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*messages, {"role": "user", "content": BRIEF_RETRY_NUDGE}]


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
