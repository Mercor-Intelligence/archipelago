"""PowerPoint formatting grading - 25% weight for formatting checks."""

import json
from typing import Any

from litellm import Choices

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.llm import call_llm

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

PPTX_FORMATTING_SYSTEM_PROMPT = """You are an expert PowerPoint evaluator grading formatting.

FORMATTING CRITERION:
{criteria}

POWERPOINT CONTENT TO EVALUATE:
{pptx_content}

TASK CONTEXT:
{prompt}

Evaluate whether the PowerPoint file meets the formatting requirements.

Respond ONLY with valid JSON:
{{
  "grade": "PASS" or "FAIL",
  "explanation": "Brief explanation for the grade"
}}"""


async def pptx_formatting_eval(input: EvalImplInput) -> VerifierResult:
    """Grade PowerPoint formatting using LLM judge against custom criteria."""
    # Extract criteria
    criteria: str = input.verifier.verifier_values.get("criteria", "")
    agent_result_path: str = input.verifier.verifier_values.get("agent_result_path", "")
    slides_to_check: str = input.verifier.verifier_values.get("slides_to_check", "")

    if not criteria:
        raise ValueError("Missing required field: criteria")

    try:
        # Get diff_result from helpers
        if not input.helper_results:
            raise ValueError("Missing helper results")

        diff_result = input.helper_results[HelperIds.SNAPSHOT_DIFF]
        changes = diff_result.get("changes", {})

        # Find PowerPoint file content
        pptx_content = ""
        if agent_result_path:
            pptx_content = (
                _find_slides_content(
                    changes, agent_result_path.lstrip("/"), slides_to_check
                )
                or ""
            )

        # If no specific path, try to find any PowerPoint file
        if not pptx_content:
            for change_type in ["created", "modified"]:
                for file_change in changes.get(change_type, []):
                    path = file_change.get("path", "")
                    if path.endswith((".pptx", ".ppt")):
                        # Get all slides if no specific slides requested
                        pptx_content = (
                            _find_slides_content(changes, path, slides_to_check)
                            or file_change.get("new_content")
                            or file_change.get("old_content")
                            or ""
                        )
                        break
                if pptx_content:
                    break

        # Extract task prompt
        prompt = ""
        for msg in input.trajectory.messages:
            if msg.get("role") == "user" and msg.get("content"):
                prompt = msg.get("content") or ""
                break

        # Build user prompt
        system_prompt = PPTX_FORMATTING_SYSTEM_PROMPT.format(
            criteria=criteria,
            pptx_content=pptx_content or "(No PowerPoint content found)",
            prompt=prompt,
        )

        # Get model and extra args
        model: str = input.grading_settings.llm_judge_model
        extra_args: dict[str, Any] = input.grading_settings.llm_judge_extra_args or {}

        # Call LLM using utility function
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Grade the PowerPoint formatting."},
        ]
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format={"type": "json_object"},
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")

        raw_content = choices[0].message.content or ""
        parsed_result = json.loads(raw_content) if raw_content else {}
        grade_raw = parsed_result.get("grade", "FAIL")
        grade = str(grade_raw).upper()
        explanation: str = str(parsed_result.get("explanation", ""))

        score = 1.0 if grade == "PASS" else 0.0

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "grade": grade,
                "explanation": explanation,
                "criteria": criteria,
            },
        )

    except Exception as e:
        raise ValueError(f"PowerPoint formatting grading failed: {str(e)}") from e


def _find_slides_content(
    changes: dict[str, Any], file_path: str, slides_spec: str
) -> str | None:
    """Find and extract content from specified slides in the diff changes."""
    slide_indices = None
    if slides_spec and slides_spec.strip():
        try:
            slide_indices = [int(s.strip()) - 1 for s in slides_spec.split(",")]
        except (ValueError, AttributeError):
            slide_indices = None

    for change_type in ["created", "modified", "unchanged"]:
        for file_change in changes.get(change_type, []):
            if file_change.get("path") == file_path:
                sub_artifacts = file_change.get("sub_artifact_changes", [])
                if not sub_artifacts:
                    return file_change.get("new_content") or file_change.get(
                        "old_content"
                    )
                if slide_indices is not None:
                    selected_slides = [
                        sub
                        for sub in sub_artifacts
                        if sub.get("index") in slide_indices
                    ]
                else:
                    selected_slides = sub_artifacts
                slide_texts = []
                for slide in selected_slides:
                    slide_idx = slide.get("index", 0)
                    slide_num = slide_idx + 1
                    title = slide.get("title", f"Slide {slide_num}")
                    content = slide.get("new_content") or slide.get("old_content", "")

                    slide_texts.append(f"=== Slide {slide_num}: {title} ===\n{content}")

                return "\n\n".join(slide_texts) if slide_texts else None

    return None
