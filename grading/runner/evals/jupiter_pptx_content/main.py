"""PowerPoint content grading - 75% weight for critical content verification."""

import json
from typing import Any

from litellm import Choices

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.llm import call_llm

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

PPTX_CONTENT_SYSTEM_PROMPT = """You are comparing a PowerPoint presentation created by an agent to a golden solution.

AGENT'S PRESENTATION:
{agent_content}

GOLDEN SOLUTION:
{golden_content}

TASK GIVEN TO AGENT:
{prompt}

CRITERIA TO VERIFY:
{criteria}

GRADING RUBRIC:
Tolerated rounding error for numerical values: +/- 0.1

Assign score 0.0 if ANY of these are true:
- Critical slide content is missing or significantly incorrect
- Key data points are wrong or outside tolerance
- Required slides are missing entirely

Assign score 0.33 if ALL of these are true:
- Critical content is present and accurate
- Supporting details missing OR multiple minor text differences OR several data points slightly off

Assign score 0.67 if ALL of these are true:
- Critical content accurate within tolerance
- Minor errors in supporting details OR single text difference OR one data point slightly off

Assign score 1.0 if ALL of these are true:
- All slide content accurate within tolerance
- All text content equivalent (abbreviations, semantic similarity, case differences OK)
- Slide structure and data match expectations

Respond ONLY with valid JSON:
{{
  "score": 0.0 | 0.33 | 0.67 | 1.0,
  "summary": "2-3 sentence explanation",
  "key_failures": ["specific mismatches"]
}}"""


async def pptx_content_eval(input: EvalImplInput) -> VerifierResult:
    """Grade PowerPoint content against golden answer with specific rubric."""
    # Extract config
    agent_result_path: str = input.verifier.verifier_values.get("agent_result_path", "")
    golden_answer_path: str = input.verifier.verifier_values.get(
        "golden_answer_path", ""
    )
    slides_to_grade: str = input.verifier.verifier_values.get("slides_to_grade", "")
    criteria: str = input.verifier.verifier_values.get("criteria", "")

    if not agent_result_path or not golden_answer_path or not criteria:
        raise ValueError("Missing required fields")

    try:
        # Get diff_result from helpers (has pre-extracted file content)
        if not input.helper_results:
            raise ValueError("Missing helper results")

        diff_result = input.helper_results[HelperIds.SNAPSHOT_DIFF]
        changes = diff_result.get("changes", {})

        # Find agent's PowerPoint file content
        agent_content = _find_slides_content(
            changes, agent_result_path.lstrip("/"), slides_to_grade
        )
        if not agent_content:
            raise ValueError(f"File not found: {agent_result_path}")

        # Find golden answer file content
        golden_content = _find_slides_content(
            changes, golden_answer_path.lstrip("/"), slides_to_grade
        )
        if not golden_content:
            raise ValueError(f"File not found: {golden_answer_path}")

        # Extract task prompt from trajectory
        prompt = ""
        for msg in input.trajectory.messages:
            if msg.get("role") == "user" and msg.get("content"):
                prompt = msg.get("content") or ""
                break

        # Build system prompt with actual file content
        system_prompt = PPTX_CONTENT_SYSTEM_PROMPT.format(
            agent_content=agent_content,
            golden_content=golden_content,
            prompt=prompt,
            criteria=criteria,
        )

        # Get model and extra args
        model: str = input.grading_settings.llm_judge_model
        extra_args: dict[str, Any] = input.grading_settings.llm_judge_extra_args or {}

        # Call LLM using utility function
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Grade the agent's PowerPoint output."},
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
        score_value = parsed_result.get("score", 0.0)
        if score_value is None:
            score_value = 0.0
        score = float(score_value)
        summary: str = str(parsed_result.get("summary", ""))
        key_failures = parsed_result.get("key_failures", [])

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "summary": summary,
                "key_failures": key_failures,
                "criteria_checked": criteria,
            },
        )

    except Exception as e:
        raise ValueError(f"PowerPoint content grading failed: {str(e)}") from e


def _find_slides_content(
    changes: dict[str, Any], file_path: str, slides_spec: str
) -> str | None:
    """
    Find and extract content from specified slides in the diff changes.

    Args:
        changes: The changes dict from diff_result
        file_path: Path to the PPTX file
        slides_spec: Comma-separated slide numbers (1-based) like "1,3,5" or "" for all

    Returns:
        Concatenated content from specified slides, or None if file not found
    """
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
