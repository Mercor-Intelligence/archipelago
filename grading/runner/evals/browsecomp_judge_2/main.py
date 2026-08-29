"""browsecomp_judge_2 eval — grades a BrowseComp response against the task's
``expected_answer`` custom field.

Unlike hle_judge (which reads the ground truth from per-task verifier values),
this eval reads ``expected_answer`` straight from the task's custom_fields at
grade time — plumbed via
``trajectory.task_custom_fields``. That lets a SINGLE world-level verifier grade
every task in the world, present and future, with zero per-task setup.

The judge model comes from the grading run's selected LLM judge. Evaluator-specific
settings remain configurable through ``eval_config_values`` (``temperature``,
``max_tokens``, ``reasoning_effort``, ``judge_max_attempts``).
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Literal

from litellm import Choices
from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from loguru import logger
from pydantic import BaseModel, model_validator

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import call_llm
from runner.utils.trajectory import (
    extract_first_user_message,
    extract_last_assistant_text,
)

from .prompt import (
    build_alias_verification_prompt,
    build_grader_prompt,
    normalize_expected_answer,
)

LLM_JUDGE_TIMEOUT = 180

# The grader model is selected by the grading run. Temperature 0 (greedy decoding) is the
# repetition-loop-prone setting: with no randomness, a model that starts
# repeating a token has nothing to knock it out of the loop, so it repeats
# until it exhausts max_tokens without ever closing the JSON verdict. A small
# amount of temperature gives the sampler a chance to escape.
DEFAULT_TEMPERATURE = 0.2
# 8192 (not 4096): the prompt's `reasoning` field is now an uncapped detailed
# explanation (the old "one short sentence, under 30 words" cap was removed to
# support alias-claim justification), and `reasoning` is still emitted before
# `extracted_final_answer`/`correct` in the required field order. A verbose
# reasoning pass at the old 4096 budget risks exhausting max_tokens before the
# JSON verdict closes — the same failure shape already described above for
# thinking-token budgets. 8192 is the value actually used in the A/B testing
# that validated this prompt/schema change (0/15 hallucinations, 0 truncations).
DEFAULT_MAX_TOKENS = 8192

# Judge calls occasionally fail transiently: empty content (a thinking-enabled
# Gemini grader exhausting its output budget, or an empty candidate), rate limits
# (429), provider overload (529), 5xx, or connection blips. A single failure used
# to hard-error the whole grade; retry with exponential backoff + decorrelated
# jitter before giving up. Overridable via eval_config_values["judge_max_attempts"].
#
# NOTE: `call_llm` is already wrapped in `@with_retry` which retries the HTTP
# transients (429/Timeout/5xx/connection) upstream. This loop's primary job is the
# empty-content case (a 200 with no body — NOT an exception, so `with_retry` never
# sees it); the HTTP-error handling here is an additional backstop.
DEFAULT_JUDGE_MAX_ATTEMPTS = 3
JUDGE_BACKOFF_BASE_S = 1.0
JUDGE_BACKOFF_CAP_S = 30.0

# Transient LLM error types worth retrying. 429 (RateLimitError) and 529
# (provider overloaded — surfaced as InternalServerError / ServiceUnavailableError
# or a bare status_code) are covered here explicitly.
_RETRYABLE_LLM_ERRORS = (
    RateLimitError,
    Timeout,
    ServiceUnavailableError,
    APIConnectionError,
    InternalServerError,
    BadGatewayError,
)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 529})


def _is_retryable(exc: Exception) -> bool:
    """Whether a failed judge attempt should be retried.

    Retries empty/unparseable responses (raised as ValueError below —
    pydantic's ValidationError is a ValueError subclass) and transient LLM
    errors including 429 rate limits and 529 provider-overload. Everything
    else (auth, context-window, genuine bad requests) fails fast.
    """
    if isinstance(exc, _RETRYABLE_LLM_ERRORS):
        return True
    if getattr(exc, "status_code", None) in _RETRYABLE_STATUS_CODES:
        return True
    return isinstance(exc, ValueError)


def _decorrelated_jitter(prev_sleep_s: float) -> float:
    """AWS-style decorrelated jitter: sleep = min(cap, uniform(base, prev*3)).

    Grows roughly exponentially (prev*3 ceiling) while the uniform draw
    decorrelates concurrent retriers so they don't stampede in lockstep.
    """
    return min(
        JUDGE_BACKOFF_CAP_S,
        random.uniform(JUDGE_BACKOFF_BASE_S, prev_sleep_s * 3),
    )


# Task custom_field keys.
EXPECTED_ANSWER_FIELD = "expected_answer"


class BrowseCompJudge2Verdict(BaseModel):
    # `is_alias_claim` first so the judge commits to it before writing
    # `reasoning` — matches the field order mandated in the prompt's "Emit
    # these fields IN THIS ORDER" rule. Deliberately carries NO evidence
    # fields: this call never has search-tool access (see module docstring),
    # so it has no way to produce real evidence. A flagged alias claim is
    # verified afterward by a separate, dedicated call (`verify_alias`).
    is_alias_claim: bool
    reasoning: str
    extracted_final_answer: str
    correct: Literal["yes", "no"]


class AliasVerificationResult(BaseModel):
    """Output of the dedicated alias-verification call (see `verify_alias`)."""

    verified: bool
    quote: str
    url: str

    @model_validator(mode="after")
    def _require_evidence_when_verified(self) -> AliasVerificationResult:
        """Deterministic backstop: `verified=True` with no evidence is a
        schema violation, not a judgment call — this call always has search
        access (fixed `ALIAS_VERIFY_MODEL`), so there's no legitimate reason
        for it to claim verification without citing what it found.
        """
        if self.verified and not (self.quote.strip() and self.url.strip()):
            raise ValueError("verified=True requires non-empty quote and url")
        return self


# Fixed model for the dedicated alias-verification call, deliberately
# DECOUPLED from `input.grading_settings.llm_judge_model` (the primary judge,
# which can be anything a world configures). Confirmed empirically to support
# combining `googleSearch` with structured output in the same call; some
# Gemini generations (e.g. gemini-2.5-flash) reject that combination outright
# (400 "controlled generation is not supported with Search tool"). Because
# this call never depends on the primary judge model choice, browsecomp_judge_2
# never needs to branch on judge-model version to decide whether search is
# safe to attach — that whole problem only existed when search was attached
# to the primary (judge-model-dependent) call.
ALIAS_VERIFY_MODEL = "vertex_ai/gemini-3.5-flash"
ALIAS_VERIFY_TIMEOUT = 120
ALIAS_VERIFY_MAX_ATTEMPTS = 3


async def verify_alias(
    *, target_answer: str, claimed_answer: str
) -> AliasVerificationResult | None:
    """Search-verify whether ``claimed_answer`` is an alias of ``target_answer``.

    The ONLY path by which browsecomp_judge_2 ever touches the internet.
    Its prompt (`build_alias_verification_prompt`) carries no context beyond
    the two strings being compared, so there is nothing else for it to search
    for — search is scoped to alias verification as a structural property of
    this call, not a prompt instruction the model has to follow.

    Returns ``None`` if the call fails after retries (transient errors);
    callers must treat that conservatively, i.e. as NOT verified.
    """
    prompt = build_alias_verification_prompt(
        target_answer=target_answer, claimed_answer=claimed_answer
    )
    messages = [{"role": "user", "content": prompt}]
    extra_args: dict[str, Any] = {
        "temperature": 0.0,
        "max_tokens": 4096,
        "tools": [{"googleSearch": {}}],
        "drop_params": True,
    }
    sleep_s = JUDGE_BACKOFF_BASE_S
    last_error: str | None = None

    for attempt in range(ALIAS_VERIFY_MAX_ATTEMPTS):
        try:
            response = await call_llm(
                model=ALIAS_VERIFY_MODEL,
                messages=messages,
                timeout=ALIAS_VERIFY_TIMEOUT,
                extra_args=extra_args,
                response_format=AliasVerificationResult,
            )
            choices = response.choices
            if not choices or not isinstance(choices[0], Choices):
                raise ValueError("LLM returned empty response")
            raw_content = choices[0].message.content
            if not raw_content:
                raise ValueError("LLM returned empty content")
            return AliasVerificationResult.model_validate_json(raw_content)
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
            retryable = _is_retryable(e)
            logger.warning(
                f"[BROWSECOMP_JUDGE_2][ALIAS_VERIFY] attempt "
                f"{attempt + 1}/{ALIAS_VERIFY_MAX_ATTEMPTS} failed "
                f"(retryable={retryable}): {e}"
            )
            if not retryable or attempt == ALIAS_VERIFY_MAX_ATTEMPTS - 1:
                break
            sleep_s = _decorrelated_jitter(sleep_s)
            await asyncio.sleep(sleep_s)
            continue

    logger.error(
        "[BROWSECOMP_JUDGE_2][ALIAS_VERIFY] verification failed after "
        f"{ALIAS_VERIFY_MAX_ATTEMPTS} attempts: {last_error}"
    )
    return None


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
    # No truncation: grade the FULL final response. The earlier 6,000-char cap
    # silently dropped the final answer of verbose models (e.g. Opus emits a
    # 10k-68k-char research narrative with the answer at the END), so the judge
    # saw only the opening deliberation and scored "no final answer" → false
    # negatives. `final_response` is a single assistant message bounded by the
    # orchestrator's output-token limit, and the judge model has a large context,
    # so passing it whole is safe.
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
        response=final_response,
    )

    cfg = input.eval_config.eval_config_values or {}
    model = input.grading_settings.llm_judge_model
    # Guard against an explicit null stored for an unset optional config field
    # (cfg.get(key, default) returns None when the key is present-but-null).
    temperature = cfg.get("temperature")
    max_tokens = cfg.get("max_tokens")
    extra_args: dict[str, Any] = {
        "temperature": DEFAULT_TEMPERATURE if temperature is None else temperature,
        "max_tokens": DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens,
    }
    # Opt-in only: omitted unless a world sets it, so existing configs keep
    # today's provider-default thinking behavior. For thinking-capable models
    # that can't fully disable reasoning (e.g. Gemini 3), thinking tokens draw
    # from the SAME max_tokens budget as the visible output — with no
    # explicit level/budget set, a verbose thinking pass can consume the
    # entire budget and leave nothing for the required JSON verdict, which
    # then fails to parse. Set to e.g. "medium" to keep some reasoning depth
    # while still leaving headroom for the verdict. Forwarded to litellm, which maps
    # it to the right provider-specific param (thinking_level/budget_tokens/
    # reasoning_effort).
    reasoning_effort = cfg.get("reasoning_effort")
    if reasoning_effort is not None:
        extra_args["reasoning_effort"] = reasoning_effort
    # No search-tool args here, deliberately: this call never touches the
    # internet, so it's never subject to the Gemini tool-vs-structured-output
    # conflict regardless of which judge model a world configures. Alias
    # verification is a separate, dedicated call (see verify_alias) fired
    # after this one succeeds.

    attempts = int(cfg.get("judge_max_attempts") or DEFAULT_JUDGE_MAX_ATTEMPTS)
    messages = [{"role": "user", "content": prompt}]
    last_error: str | None = None
    sleep_s = JUDGE_BACKOFF_BASE_S

    for attempt in range(attempts):
        try:
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
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
            retryable = _is_retryable(e)
            logger.warning(
                f"[BROWSECOMP_JUDGE_2] attempt {attempt + 1}/{attempts} failed "
                f"(retryable={retryable}): {e}"
            )
            # Fail fast on non-retryable errors; otherwise back off (exponential +
            # decorrelated jitter) and retry until attempts are exhausted.
            if not retryable or attempt == attempts - 1:
                break
            sleep_s = _decorrelated_jitter(sleep_s)
            await asyncio.sleep(sleep_s)
            continue

        # Primary grading call succeeded. If it flagged a plausible alias,
        # verify it with the dedicated search call before finalizing --
        # `correct`/`reasoning` from the primary call assumed the alias holds,
        # so an unconfirmed (or unverifiable) claim must flip the grade
        # rather than silently pass through on an unproven assertion.
        correct = parsed.correct
        reasoning = parsed.reasoning
        alias_evidence_quote = ""
        alias_evidence_url = ""

        if parsed.is_alias_claim:
            verification = await verify_alias(
                target_answer=target_answer,
                claimed_answer=parsed.extracted_final_answer,
            )
            if verification is None:
                correct = "no"
                reasoning = (
                    f"{reasoning} [Alias claim could not be verified: the "
                    "verification check failed after retries.]"
                )
            elif verification.verified:
                alias_evidence_quote = verification.quote
                alias_evidence_url = verification.url
            else:
                correct = "no"
                reasoning = (
                    f"{reasoning} [Alias claim could not be verified via web "
                    "search; treating the names as different entities and "
                    "scoring incorrect.]"
                )

        score = 1.0 if correct == "yes" else 0.0
        logger.info(
            f"[BROWSECOMP_JUDGE_2] correct={correct} score={score} "
            f"| extracted={parsed.extracted_final_answer!r} (attempt {attempt + 1})"
        )
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "extracted_final_answer": parsed.extracted_final_answer,
                "reasoning": reasoning,
                "correct": correct,
                "grader_model": model,
                "is_alias_claim": parsed.is_alias_claim,
                "alias_evidence_quote": alias_evidence_quote,
                "alias_evidence_url": alias_evidence_url,
            },
        )

    # Every attempt returned empty/unparseable — give up and record the error.
    logger.error(
        f"[BROWSECOMP_JUDGE_2] judge call failed after {attempts} attempts: {last_error}"
    )
    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=0.0,
        status=VerifierResultStatus.ERROR,
        verifier_result_values={"error": last_error or "unknown"},
        message=f"browsecomp_judge_2 failed after {attempts} attempts: {last_error}",
    )
