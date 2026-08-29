"""PPTX style verifier — batched multi-criteria LLM grading for slide decks.

Groups criteria by (scope, judge_mode) and issues batched LLM calls.
Supports text mode (extracted font/layout metadata) and image mode (rendered
slide PNGs).
"""

import asyncio
import json
import zipfile
from typing import Any

from litellm import Choices
from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult
from runner.utils.file_transformations.pptx_to_images.main import pptx_to_images
from runner.utils.file_transformations.pptx_to_style_metadata.main import (
    pptx_to_style_metadata,
)
from runner.utils.llm import build_messages, call_llm

from .models import CriterionResult, StyleCriterion

LOG_PREFIX = "PPTX_STYLE"
LLM_JUDGE_TIMEOUT = 3600
MAX_JSON_RETRIES = 3

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

TEXT_SYSTEM_PROMPT = """\
You are an expert presentation evaluator. You grade PowerPoint slide decks on \
style quality using structured font/layout metadata extracted from the file.

For each criterion listed below, return a JSON object whose keys are the \
criterion names and whose values are objects with "score" (integer 0 or 1, \
or the string "nan" if the criterion is not applicable) and "rationale" \
(a brief explanation).

CRITERIA:
{criteria_block}

Respond ONLY with valid JSON matching this schema:
{{
  "<criteria_name>": {{"score": 0 | 1 | "nan", "rationale": "..."}},
  ...
}}"""

IMAGE_SYSTEM_PROMPT = """\
You are an expert presentation evaluator. You grade PowerPoint slide decks on \
style quality by examining rendered slide images.

For each criterion listed below, return a JSON object whose keys are the \
criterion names and whose values are objects with "score" (integer 0 or 1, \
or the string "nan" if the criterion is not applicable) and "rationale" \
(a brief explanation).

CRITERIA:
{criteria_block}

Respond ONLY with valid JSON matching this schema:
{{
  "<criteria_name>": {{"score": 0 | 1 | "nan", "rationale": "..."}},
  ...
}}"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_pptx_in_snapshot(
    snapshot_bytes: Any,
    preferred_path: str | None,
) -> tuple[str, bytes]:
    """Locate and read a PPTX file from a snapshot zip.

    Args:
        snapshot_bytes: BytesIO with the snapshot zip.
        preferred_path: Optional path hint from verifier config.

    Returns:
        (file_path, file_bytes) tuple.

    Raises:
        ValueError: If no PPTX file is found.
    """
    snapshot_bytes.seek(0)
    with zipfile.ZipFile(snapshot_bytes, "r") as zf:
        names = zf.namelist()

        # Try preferred path first (with and without leading slash)
        if preferred_path:
            candidates = [
                preferred_path,
                preferred_path.lstrip("/"),
                f"/{preferred_path}",
            ]
            for c in candidates:
                if c in names:
                    return c, zf.read(c)

        # Auto-detect first .pptx file
        for name in names:
            if name.lower().endswith(".pptx") and not name.startswith("__MACOSX"):
                return name, zf.read(name)

    raise ValueError(
        "No PPTX file found in snapshot"
        + (f" (tried preferred path: {preferred_path})" if preferred_path else "")
    )


def _format_criteria_block(criteria: list[StyleCriterion]) -> str:
    """Format criteria into a numbered list for the prompt."""
    lines: list[str] = []
    for i, c in enumerate(criteria, 1):
        prefix = f"{i}. [{c.criteria_name}]"
        lines.append(f"{prefix}: {c.criteria_prompt}")
        if c.judge_prompt:
            lines.append(f"   Additional context: {c.judge_prompt}")
    return "\n".join(lines)


def _parse_judge_response(
    raw_json: str,
    criteria: list[StyleCriterion],
) -> dict[str, CriterionResult]:
    """Parse the LLM JSON response into CriterionResult objects."""
    parsed = json.loads(raw_json)
    results: dict[str, CriterionResult] = {}

    for c in criteria:
        entry = parsed.get(c.criteria_name, {})
        raw_score = entry.get("score", "nan")
        rationale = str(entry.get("rationale", ""))

        if isinstance(raw_score, str) and raw_score.lower() == "nan":
            score = -1.0  # sentinel for nan
        else:
            try:
                score = float(raw_score)
                # Clamp to [0, 1]
                score = max(0.0, min(1.0, score))
            except (ValueError, TypeError):
                score = -1.0

        results[c.criteria_name] = CriterionResult(score=score, rationale=rationale)

    return results


async def _call_judge(
    model: str,
    extra_args: dict[str, Any],
    criteria: list[StyleCriterion],
    judge_mode: str,
    context_text: str | None,
    context_images: list[dict[str, Any]] | None,
    scope_label: str,
    task_id: str,
) -> dict[str, CriterionResult]:
    """Issue a single batched LLM call for a group of criteria.

    Returns a dict mapping criteria_name -> CriterionResult.
    """
    criteria_block = _format_criteria_block(criteria)

    if judge_mode == "image":
        system_prompt = IMAGE_SYSTEM_PROMPT.format(criteria_block=criteria_block)
        user_prompt = f"Grade the following slide(s) for the criteria above.\nScope: {scope_label}"
        messages = build_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=context_images,
        )
    else:
        system_prompt = TEXT_SYSTEM_PROMPT.format(criteria_block=criteria_block)
        user_prompt = (
            f"Grade the following presentation metadata for the criteria above.\n"
            f"Scope: {scope_label}\n\n"
            f"METADATA:\n{context_text}"
        )
        messages = build_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    # Retry loop for JSON parsing
    for attempt in range(MAX_JSON_RETRIES):
        logger.info(
            f"[{LOG_PREFIX}] task={task_id} | scope={scope_label} | "
            f"mode={judge_mode} | attempt={attempt + 1}/{MAX_JSON_RETRIES}"
        )
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format={"type": "json_object"},
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            continue

        raw_content = choices[0].message.content or ""
        try:
            return _parse_judge_response(raw_content, criteria)
        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | JSON parse error attempt "
                f"{attempt + 1}: {e}"
            )

    # If all retries fail, return nan for everything
    return {
        c.criteria_name: CriterionResult(
            score=-1.0, rationale="Failed to parse LLM response"
        )
        for c in criteria
    }


# ---------------------------------------------------------------------------
# Main eval
# ---------------------------------------------------------------------------


async def pptx_style_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Evaluate a PPTX file against multiple style criteria."""
    task_id = input.verifier.task_id or "unknown"

    # 1. Parse criteria from verifier config
    raw_criteria = input.verifier.verifier_values.get("pptx_style_criteria", [])
    if not raw_criteria:
        raise ValueError("No pptx_style_criteria configured")

    criteria = [StyleCriterion(**c) for c in raw_criteria]
    agent_result_path = input.verifier.verifier_values.get("agent_result_path")

    # 2. Separate by scope
    deck_criteria = [c for c in criteria if c.criteria_scope == "deck"]
    slide_criteria = [c for c in criteria if c.criteria_scope == "slide"]

    # 3. Find PPTX in snapshot
    try:
        pptx_path, pptx_bytes = _find_pptx_in_snapshot(
            input.final_snapshot_bytes, agent_result_path
        )
    except ValueError as e:
        logger.warning(f"[{LOG_PREFIX}] task={task_id} | {e}")
        input.final_snapshot_bytes.seek(0)
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "error": str(e),
                "criteria_count": len(criteria),
            },
        )
    logger.info(f"[{LOG_PREFIX}] task={task_id} | Found PPTX: {pptx_path}")

    # 4. Extract metadata and images based on what modes are needed
    needs_text = any(c.judge_mode == "text" for c in criteria)
    needs_image = any(c.judge_mode == "image" for c in criteria)

    style_metadata: dict[str, Any] | None = None
    slide_images: list[dict[str, Any]] = []

    if needs_text:
        try:
            style_metadata = await asyncio.to_thread(
                pptx_to_style_metadata, pptx_bytes, pptx_path
            )
        except Exception as e:
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | Text metadata extraction failed: {e}"
            )
            # Text-mode criteria will get error results below

    if needs_image:
        try:
            transform_output = await pptx_to_images(pptx_bytes, pptx_path)
            slide_images = [
                {"url": img.url, "placeholder": img.placeholder}
                for img in transform_output.images
            ]
        except Exception as e:
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | Image extraction failed: {e}"
            )
            # Image-mode criteria will get error results below

    metadata_json = json.dumps(style_metadata, indent=2) if style_metadata else "{}"
    slide_count = style_metadata["slide_count"] if style_metadata else len(slide_images)

    # 5. Build batched LLM calls
    model: str = input.grading_settings.llm_judge_model
    extra_args: dict[str, Any] = input.grading_settings.llm_judge_extra_args or {}

    call_tasks: list[tuple[str, Any]] = []  # (result_key, coroutine)

    # Group deck criteria by judge_mode
    deck_text = [c for c in deck_criteria if c.judge_mode == "text"]
    deck_image = [c for c in deck_criteria if c.judge_mode == "image"]

    if deck_text:
        if style_metadata:
            call_tasks.append(
                (
                    "deck",
                    _call_judge(
                        model,
                        extra_args,
                        deck_text,
                        "text",
                        context_text=metadata_json,
                        context_images=None,
                        scope_label="full deck (text)",
                        task_id=task_id,
                    ),
                )
            )
        else:
            call_tasks.append(
                (
                    "deck",
                    _make_error_results(
                        deck_text, "Text metadata extraction unavailable"
                    ),
                )
            )
    if deck_image:
        if slide_images:
            call_tasks.append(
                (
                    "deck",
                    _call_judge(
                        model,
                        extra_args,
                        deck_image,
                        "image",
                        context_text=None,
                        context_images=slide_images,
                        scope_label="full deck (image)",
                        task_id=task_id,
                    ),
                )
            )
        else:
            # No images available — mark criteria as nan
            call_tasks.append(
                (
                    "deck",
                    _make_error_results(deck_image, "Image extraction unavailable"),
                )
            )

    # Group slide criteria by judge_mode, issue per-slide calls
    slide_text = [c for c in slide_criteria if c.judge_mode == "text"]
    slide_image = [c for c in slide_criteria if c.judge_mode == "image"]

    for slide_idx in range(slide_count):
        slide_key = f"slide_{slide_idx + 1}"

        if slide_text and style_metadata:
            slide_meta: dict[str, Any] = (
                style_metadata["slides"][slide_idx]
                if slide_idx < len(style_metadata["slides"])
                else {}
            )
            call_tasks.append(
                (
                    slide_key,
                    _call_judge(
                        model,
                        extra_args,
                        slide_text,
                        "text",
                        context_text=json.dumps(slide_meta, indent=2),
                        context_images=None,
                        scope_label=f"slide {slide_idx + 1} (text)",
                        task_id=task_id,
                    ),
                )
            )
        elif slide_text:
            call_tasks.append(
                (
                    slide_key,
                    _make_error_results(
                        slide_text, "Text metadata extraction unavailable"
                    ),
                )
            )

        if slide_image:
            if slide_idx < len(slide_images):
                call_tasks.append(
                    (
                        slide_key,
                        _call_judge(
                            model,
                            extra_args,
                            slide_image,
                            "image",
                            context_text=None,
                            context_images=[slide_images[slide_idx]],
                            scope_label=f"slide {slide_idx + 1} (image)",
                            task_id=task_id,
                        ),
                    )
                )
            else:
                call_tasks.append(
                    (
                        slide_key,
                        _make_error_results(
                            slide_image,
                            f"No image available for slide {slide_idx + 1}",
                        ),
                    )
                )

    # Handle case where slide_count is 0 but slide-scoped criteria exist
    if slide_count == 0 and (slide_text or slide_image):
        error_criteria = slide_text + slide_image
        call_tasks.append(
            (
                "slide_unknown",
                _make_error_results(
                    error_criteria,
                    "Could not determine slide count — extraction failed",
                ),
            )
        )

    # 6. Execute all calls concurrently
    if call_tasks:
        keys = [k for k, _ in call_tasks]
        coros = [c for _, c in call_tasks]
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        # Merge results by key (multiple calls may share a key, e.g. "deck")
        all_results: dict[str, dict[str, Any]] = {}
        for key, result in zip(keys, raw_results, strict=True):
            if isinstance(result, BaseException):
                logger.error(f"[{LOG_PREFIX}] task={task_id} | {key} failed: {result}")
                continue
            existing = all_results.get(key, {})
            for cname, cresult in result.items():
                existing[cname] = {
                    "score": "nan" if cresult.score == -1.0 else cresult.score,
                    "rationale": cresult.rationale,
                }
            all_results[key] = existing
    else:
        all_results = {}

    # 7. Aggregate scores
    deck_scores: list[float] = []
    slide_scores_by_slide: dict[str, list[float]] = {}

    for key, criterion_results in all_results.items():
        for _cname, entry in criterion_results.items():
            raw_score = entry["score"]
            if raw_score == "nan":
                continue
            score_val = float(raw_score)
            if key == "deck":
                deck_scores.append(score_val)
            else:
                slide_scores_by_slide.setdefault(key, []).append(score_val)

    deck_avg = sum(deck_scores) / len(deck_scores) if deck_scores else None
    per_slide_avgs: list[float] = []
    for slide_key in sorted(slide_scores_by_slide.keys()):
        scores = slide_scores_by_slide[slide_key]
        if scores:
            per_slide_avgs.append(sum(scores) / len(scores))

    slide_avg = sum(per_slide_avgs) / len(per_slide_avgs) if per_slide_avgs else None

    # Final score: average of deck and slide averages (use whichever exists)
    if deck_avg is not None and slide_avg is not None:
        final_score = (deck_avg + slide_avg) / 2
    elif deck_avg is not None:
        final_score = deck_avg
    elif slide_avg is not None:
        final_score = slide_avg
    else:
        final_score = 0.0

    # Reset snapshot seek position
    input.final_snapshot_bytes.seek(0)

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=final_score,
        verifier_result_values={
            "detailed_results": all_results,
            "deck_score": deck_avg,
            "slide_average": slide_avg,
            "final_score": final_score,
            "criteria_count": len(criteria),
            "slide_count": slide_count,
        },
    )


async def _make_error_results(
    criteria: list[StyleCriterion],
    error_msg: str,
) -> dict[str, CriterionResult]:
    """Create nan results for criteria that cannot be evaluated."""
    return {
        c.criteria_name: CriterionResult(score=-1.0, rationale=error_msg)
        for c in criteria
    }
