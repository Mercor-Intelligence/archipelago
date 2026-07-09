"""browsecomp_judge_2 eval — grades a BrowseComp response against the task's
``expected_answer`` custom field.

Unlike hle_judge (which reads the ground truth from per-task verifier values),
this eval reads ``expected_answer`` straight from the task's custom_fields at
grade time — plumbed via
``trajectory.task_custom_fields``. That lets a SINGLE world-level verifier grade
every task in the world, present and future, with zero per-task setup.

The judge model defaults to ``claude-sonnet-4-6`` @ temperature 0, overridable
via the eval config's ``eval_config_values``.
"""

from __future__ import annotations

from typing import Any, Literal

from litellm import Choices
from loguru import logger
from pydantic import BaseModel

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import call_llm
from runner.utils.trajectory import (
    extract_first_user_message,
    extract_last_assistant_text,
)

from .prompt import MAX_RESPONSE_CHARS, build_grader_prompt, normalize_expected_answer

LLM_JUDGE_TIMEOUT = 180

# Default grader is claude-sonnet-4-6 @ temperature 0. Overridable per-world
# via eval_config.eval_config_values.
DEFAULT_GRADER_MODEL = "anthropic/claude-sonnet-4-6"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 4096

# Task custom_field keys.
EXPECTED_ANSWER_FIELD = "expected_answer"


class BrowseCompJudge2Verdict(BaseModel):
    # `reasoning` first so the judge reasons before deciding (soft CoT).
    reasoning: str
    extracted_final_answer: str
    correct: Literal["yes", "no"]


def _err(input: EvalImplInput, message: str) -> VerifierResult:
    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=0.0,
        status=VerifierResultStatus.ERROR,
        verifier_result_values={},
        message=message,
    )


async def browsecomp_judge_2_eval(input: EvalImplInput) -> VerifierResult:
    """Grade one BrowseComp response against the task's expected_answer."""
    task_fields = input.trajectory.task_custom_fields or {}

    # Ground truth: the expected_answer custom field ONLY (no fallback to any
    # other answer field).
    target_answer = normalize_expected_answer(
        str(task_fields.get(EXPECTED_ANSWER_FIELD, "") or "")
    )
    if not target_answer:
        # No ground truth → not gradeable. Returning ERROR (rather than score 0)
        # keeps answerless tasks out of accuracy instead of counting them as a
        # model failure; the grading run will be marked ERROR.
        return _err(
            input,
            f"Task has no '{EXPECTED_ANSWER_FIELD}' custom field — not gradeable "
            "by browsecomp_judge_2 (no ground-truth answer).",
        )

    question = extract_first_user_message(input.trajectory)
    final_response = extract_last_assistant_text(input.trajectory)

    if not final_response:
        logger.info("[BROWSECOMP_JUDGE_2] no assistant response → score=0.0")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "extracted_final_answer": "None",
                "reasoning": "No assistant response found in trajectory.",
                "correct": "no",
            },
        )

    prompt = build_grader_prompt(
        question=question,
        target_answer=target_answer,
        response=final_response[:MAX_RESPONSE_CHARS],
    )

    cfg = input.eval_config.eval_config_values or {}
    model = str(cfg.get("grader_model") or DEFAULT_GRADER_MODEL)
    # Guard against an explicit null stored for an unset optional config field
    # (cfg.get(key, default) returns None when the key is present-but-null).
    temperature = cfg.get("temperature")
    max_tokens = cfg.get("max_tokens")
    extra_args: dict[str, Any] = {
        "temperature": DEFAULT_TEMPERATURE if temperature is None else temperature,
        "max_tokens": DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens,
    }

    try:
        messages = [{"role": "user", "content": prompt}]
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format=BrowseCompJudge2Verdict,
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")
        raw_content = choices[0].message.content
        if not raw_content:
            raise ValueError("LLM returned empty content")

        parsed = BrowseCompJudge2Verdict.model_validate_json(raw_content)
        score = 1.0 if parsed.correct == "yes" else 0.0

        logger.info(
            f"[BROWSECOMP_JUDGE_2] correct={parsed.correct} score={score} "
            f"| extracted={parsed.extracted_final_answer!r}"
        )
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "extracted_final_answer": parsed.extracted_final_answer,
                "reasoning": parsed.reasoning,
                "correct": parsed.correct,
                "grader_model": model,
            },
        )
    except Exception as e:
        logger.error(f"[BROWSECOMP_JUDGE_2] judge call failed: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"error": str(e)},
            message=f"browsecomp_judge_2 failed: {str(e)}",
        )
