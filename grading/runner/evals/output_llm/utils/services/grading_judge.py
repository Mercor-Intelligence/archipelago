import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import ValidationError

from runner.evals.output_llm.utils.prompts import (
    ExtractedValueGradingResponseSchema,
    GradingResponseSchema,
)
from runner.evals.output_llm.utils.shared import MAX_JSON_RETRIES
from runner.utils.llm import call_llm as default_call_llm

CallLlm = Callable[..., Awaitable[Any]]
ExtraArgsForAttempt = Callable[[dict[str, Any] | None, int], dict[str, Any] | None]
_MAX_FAILURE_CHARS = 500
_EXTRACTED_VALUE_KEY = re.compile(r'(?<!\\)"extracted_value"')

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
    response_schema: type[GradingResponseSchema] = GradingResponseSchema,
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
            response_format=response_schema,
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
            parsed = parse_grading_response(raw_content, response_schema)
            break
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            last_error = str(e)
            if not isinstance(e, ValidationError):
                parsed = salvage_verdict(raw_content, response_schema)
                if parsed is not None:
                    logger.warning(
                        f"{log_prefix} task={task_id} kept the verdict of a "
                        f"malformed reply: finish_reason={finish_reason} "
                        f"error={last_error[:_MAX_FAILURE_CHARS]}"
                    )
                    break
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


async def run_grading_judge_votes(
    *, votes: int, task_id: str, log_prefix: str = "[JUDGE]", **judge_kwargs: Any
) -> tuple[GradingResponseSchema, str | None, dict[str, int]]:
    """``votes`` independent judge calls; returns a majority vote's verdict and a tally.

    A vote that fails counts for neither side, so with failures there may be no
    majority of all ``votes``; that raises like a single failed judge.
    """
    outcomes = await asyncio.gather(
        *(
            run_grading_judge(task_id=task_id, log_prefix=log_prefix, **judge_kwargs)
            for _ in range(votes)
        ),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
    ballots = [o for o in outcomes if not isinstance(o, BaseException)]
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    met = sum(parsed.is_criteria_true for parsed, _ in ballots)
    not_met = len(ballots) - met
    tally = {"met": met, "not_met": not_met, "failed": len(errors), "votes": votes}
    logger.info(f"{log_prefix} task={task_id} votes: {tally}")
    if met * 2 > votes:
        verdict = True
    elif not_met * 2 > votes:
        verdict = False
    else:
        cause = errors[0] if errors else None
        detail = f"; first error: {str(cause)[:_MAX_FAILURE_CHARS]}" if cause else ""
        raise ValueError(f"No majority verdict ({tally}){detail}") from cause
    majority = [b for b in ballots if b[0].is_criteria_true is verdict]
    parsed, raw_content = majority[0]
    if isinstance(parsed, ExtractedValueGradingResponseSchema):
        value = next(
            (
                vote.extracted_value
                for vote, _ in majority
                if isinstance(vote, ExtractedValueGradingResponseSchema)
                and vote.extracted_value is not None
            ),
            None,
        )
        parsed = parsed.model_copy(update={"extracted_value": value})
    return parsed, raw_content, tally


def _missing_verdict(error: Exception) -> bool:
    return isinstance(error, ValidationError) and any(
        err["type"] == "missing" and err["loc"] == ("is_criteria_true",)
        for err in error.errors()
    )


def _brief_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*messages, {"role": "user", "content": BRIEF_RETRY_NUDGE}]


def _reports_value(schema: type[GradingResponseSchema]) -> bool:
    return issubclass(schema, ExtractedValueGradingResponseSchema)


def salvage_verdict(
    raw_content: str, schema: type[GradingResponseSchema]
) -> GradingResponseSchema | None:
    """Verdict of a malformed reply (value if complete); None for the base schema."""
    if not _reports_value(schema):
        return None
    try:
        parsed = parse_grading_response(
            raw_content.rstrip().removesuffix(",") + "}", schema
        )
    except (json.JSONDecodeError, ValidationError, ValueError):
        pass
    else:
        if not raw_content[-1:].isdigit():  # a number at the very end may be cut off
            return parsed
        return parsed.model_copy(update={"extracted_value": None})
    keys = [match.start() for match in _EXTRACTED_VALUE_KEY.finditer(raw_content)]
    comma = raw_content.rfind(",")
    heads = [raw_content[:key] for key in reversed(keys)] + [raw_content]
    heads += [raw_content[:comma]] if comma >= 0 else []
    for head in heads:
        closed = head.rstrip().removesuffix(",") + "}"
        try:
            parsed = parse_grading_response(closed, schema)
        except (json.JSONDecodeError, ValidationError, ValueError):
            continue
        return parsed.model_copy(update={"extracted_value": None})
    return None


def parse_grading_response(
    raw_content: str, schema: type[GradingResponseSchema] = GradingResponseSchema
) -> GradingResponseSchema:
    raw_json = _load_first_json_object(raw_content)
    if isinstance(raw_json.get("rationale"), dict):
        raw_json["rationale"] = json.dumps(raw_json["rationale"])
    return schema.model_validate(raw_json)


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
