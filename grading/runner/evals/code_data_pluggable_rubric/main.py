"""Eval implementation — orchestrates one judge call per criterion.

Resolves the three plug points (artifact extractors, judge prompt, weight
scheme) from eval_config and verifier_values, composes the agent_artifact and
task_context, calls the judge, parses the verdict, and emits a weighted score.
"""

from __future__ import annotations

import json
import re
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import ValidationError

from runner.evals.models import EvalImplInput
from runner.models import Verifier, VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages, call_llm

from .artifact_extractors import (
    MISSING_ARTIFACT,
    compose_artifacts,
)
from .models import CriterionVerdict
from .prompts import PROMPT_REGISTRY

# ---------------------------------------------------------------------------
# Judge I/O — call the judge and parse a structured verdict.
# ---------------------------------------------------------------------------

LLM_JUDGE_TIMEOUT = 3600
MAX_JSON_RETRIES = 5

_INVALID_ESCAPE_RE = re.compile(r"\\(?![\"\\/bfnrtu])")


def _repair_json(text: str) -> str:
    """Fix common JSON issues from LLM output (unescaped backslashes)."""
    if "\\" not in text:
        return text
    return _INVALID_ESCAPE_RE.sub(r"\\\\", text)


def _extract_json_object(text: str) -> str | None:
    """Find the first balanced {...} block in text."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


async def _parse_verdict_with_retry(
    *,
    model: str,
    messages: list[dict[str, Any]],
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> CriterionVerdict:
    """Call the judge with JSON-parse retries; return a parsed CriterionVerdict."""
    last_error: Exception | None = None
    raw: str | None = None

    for attempt in range(MAX_JSON_RETRIES):
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format={"type": "json_object"},
        )
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            last_error = ValueError("empty choices")
            continue

        raw = choices[0].message.content or ""

        extracted = _extract_json_object(raw)
        candidates = [raw]
        if extracted is not None and extracted != raw:
            candidates.append(extracted)
        candidates.extend(_repair_json(c) for c in list(candidates))

        for try_text in candidates:
            try:
                return CriterionVerdict.model_validate_json(try_text)
            except (ValidationError, json.JSONDecodeError) as e:
                last_error = e

        logger.warning(
            f"[code_rubric][JUDGE] task={task_id} | attempt {attempt + 1}/"
            f"{MAX_JSON_RETRIES} failed to parse verdict: {last_error}"
        )

    raise ValueError(
        f"Failed to parse judge verdict after {MAX_JSON_RETRIES} attempts. "
        f"Last error: {last_error}. Raw: {raw!r}"
    )


# ---------------------------------------------------------------------------
# Weight schemes — turn (verdict, weight_label) into a numeric score.
#
#     score = (1 if verdict.passed else 0) * factors[weight_label]
#
# Penalty labels carry a negative factor — the judge marks passed=True when
# the error IS present, so the multiplication produces a negative contribution.
# ---------------------------------------------------------------------------

WEIGHT_SCHEME_REGISTRY: dict[str, dict[str, Any]] = {
    "major_minor_1_0_5": {
        "factors": {"major": 1.0, "minor": 0.5},
        "default_severity": "major",
    },
    "critical_bonus_penalty": {
        "factors": {"critical": 1.0, "bonus": 1.0, "penalty": -1.0},
        "default_severity": "critical",
    },
}


def score_for(
    scheme: dict[str, Any], verdict: CriterionVerdict, weight_label: str
) -> float:
    """Apply the scheme's factor table to a single verdict."""
    factor = scheme["factors"].get(weight_label.lower(), 1.0)
    return (1.0 if verdict.passed else 0.0) * factor


def severity_options(scheme: dict[str, Any]) -> list[str]:
    """Severity labels admitted by this scheme."""
    return list(scheme["factors"].keys())


def _coerce_id_list(raw: Any) -> list[str]:
    """Normalize raw config (None | str | list) into list[str], stripped, non-empty."""
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _resolve(vv: dict[str, Any], cfg: dict[str, Any], key: str) -> Any:
    """Per-criterion override > world default for a config key."""
    return vv.get(key) or cfg.get(key)


def _error_result(
    verifier: Verifier,
    msg: str,
    **extra: Any,
) -> VerifierResult:
    """Build an ERROR-status VerifierResult with score 0 and a uniform shape."""
    return VerifierResult(
        verifier_id=verifier.verifier_id,
        verifier_version=verifier.verifier_version,
        score=0.0,
        status=VerifierResultStatus.ERROR,
        verifier_result_values={"error": msg, **extra},
        message=msg,
    )


async def code_data_pluggable_rubric_eval(
    input: EvalImplInput,
    weight_scheme_id: str,
) -> VerifierResult:
    """Grade one rubric criterion. Bound to a weight scheme by `make_eval_impl`."""
    vv = input.verifier.verifier_values or {}
    cfg = input.eval_config.eval_config_values or {}
    task_id = input.verifier.task_id or "unknown"

    # 1. Resolve plug points (per-criterion override > world default).
    criterion = (vv.get("criteria") or "").strip()
    prompt_id = (_resolve(vv, cfg, "prompt_id") or "").strip()
    artifact_ids = _coerce_id_list(_resolve(vv, cfg, "artifact_ids"))
    task_context_ids = _coerce_id_list(_resolve(vv, cfg, "task_context_artifact_ids"))
    scheme = WEIGHT_SCHEME_REGISTRY.get(weight_scheme_id)
    prompt = PROMPT_REGISTRY.get(prompt_id)

    # 2. Validate.
    if not criterion:
        return _error_result(input.verifier, "Missing required field: criteria")
    if scheme is None:
        return _error_result(
            input.verifier, f"Unknown weight_scheme_id: {weight_scheme_id!r}"
        )
    if prompt is None:
        return _error_result(input.verifier, f"Unknown prompt_id: {prompt_id!r}")
    if not artifact_ids:
        return _error_result(input.verifier, "No agent_artifact sources configured")

    # 3. Apply the criterion's severity label, falling back to the scheme default.
    weight_label = (
        (vv.get("weight_label") or scheme["default_severity"]).strip().lower()
    )
    if weight_label not in scheme["factors"]:
        logger.warning(
            f"[code_rubric] task={task_id} | weight_label {weight_label!r} not in scheme "
            f"{weight_scheme_id}; using default {scheme['default_severity']!r}"
        )
        weight_label = scheme["default_severity"]

    # 4. Compose inputs. Missing agent_artifact is fatal; missing task_context is fine.
    agent_artifact, art_present, art_missing = await compose_artifacts(
        input, artifact_ids
    )
    if agent_artifact == MISSING_ARTIFACT:
        return _error_result(
            input.verifier,
            f"All agent_artifact sources missing: {artifact_ids}",
            artifact_ids_requested=artifact_ids,
            artifact_ids_missing=art_missing,
        )

    task_context, ctx_present, _ = (
        await compose_artifacts(input, task_context_ids)
        if task_context_ids
        else ("", [], [])
    )
    if task_context == MISSING_ARTIFACT:
        task_context = ""

    # 5. Build prompt + call judge.
    user_prompt = prompt.build_judge_prompt(
        criterion=criterion,
        rationale=vv.get("rationale") or None,
        category=vv.get("category") or [],
        agent_artifact=agent_artifact,
        task_context=task_context,
        weight_label=weight_label,
    )
    messages = build_messages(
        system_prompt=prompt.system_prompt, user_prompt=user_prompt
    )

    try:
        verdict = await _parse_verdict_with_retry(
            model=input.grading_settings.llm_judge_model,
            messages=messages,
            extra_args=input.grading_settings.llm_judge_extra_args,
            task_id=task_id,
        )
    except Exception as e:
        logger.exception(f"[code_rubric][JUDGE] task={task_id} | judge failed")
        return _error_result(input.verifier, f"Judge failed: {e}")

    # 6. Score and return.
    weighted_score = score_for(scheme, verdict, weight_label)
    judge_grade = "pass" if verdict.passed else "fail"

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=weighted_score,
        verifier_result_values={
            "judge_grade": judge_grade,
            "grade_rationale": verdict.reason,
            "weighted_score": weighted_score,
        },
        message=(
            f"prompt={prompt.id} agent_artifact={art_present} "
            f"task_context={ctx_present} scheme={weight_scheme_id} "
            f"label={weight_label} verdict={judge_grade} score={weighted_score}"
        ),
    )


def make_eval_impl(weight_scheme_id: str):
    """Build a 1-arg eval_impl bound to a specific weight scheme."""

    async def _impl(input: EvalImplInput) -> VerifierResult:
        return await code_data_pluggable_rubric_eval(input, weight_scheme_id)

    _impl.__name__ = f"code_data_pluggable_rubric_eval__{weight_scheme_id}"
    return _impl
