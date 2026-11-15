"""Excel content grading - 75% weight for critical values verification."""

import json
from typing import Any

from litellm import Choices

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.llm import call_llm

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

EXCEL_CONTENT_SYSTEM_PROMPT = """You are comparing a spreadsheet created by an agent to a golden solution.

AGENT'S OUTPUT:
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
- At least one critical value is missing or outside tolerated rounding error
- ALL calculated values are hardcoded (no formulas)

Assign score 0.33 if ALL of these are true:
- Critical values within rounding error thresholds
- Other supporting values missing OR text values notably different for multiple cells OR majority (but not all) values hardcoded

Assign score 0.67 if ALL of these are true:
- Critical numerical values within rounding error thresholds
- Supporting numerical values inaccurate OR text values different for a single cell OR a few (not majority) values hardcoded

Assign score 1.0 if ALL of these are true:
- All numerical values within rounding error thresholds
- All text values equivalent (abbreviations, semantic similarity, case differences OK)
- Calculated values use formulas (not hardcoded)

Respond ONLY with valid JSON:
{{
  "score": 0.0 | 0.33 | 0.67 | 1.0,
  "summary": "2-3 sentence explanation",
  "key_failures": ["specific mismatches"]
}}"""


async def excel_content_eval(input: EvalImplInput) -> VerifierResult:
    """Grade Excel content against golden answer with specific rubric."""
    # Extract config
    agent_result_path: str = input.verifier.verifier_values.get("agent_result_path", "")
    golden_answer_path: str = input.verifier.verifier_values.get(
        "golden_answer_path", ""
    )
    tab_to_grade: str = input.verifier.verifier_values.get("tab_to_grade", "")
    criteria: str = input.verifier.verifier_values.get("criteria", "")

    if not agent_result_path or not golden_answer_path or not criteria:
        raise ValueError("Missing required fields")

    try:
        # Get diff_result from helpers (has pre-extracted file content)
        if not input.helper_results:
            raise ValueError("Missing helper results")

        diff_result = input.helper_results[HelperIds.SNAPSHOT_DIFF]
        changes = diff_result.get("changes", {})

        # Find agent's Excel file content
        agent_content = _find_file_content(
            changes, agent_result_path.lstrip("/"), tab_to_grade
        )
        if not agent_content:
            raise ValueError(f"File not found: {agent_result_path}")

        # Find golden answer file content
        golden_content = _find_file_content(
            changes, golden_answer_path.lstrip("/"), tab_to_grade
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
        system_prompt = EXCEL_CONTENT_SYSTEM_PROMPT.format(
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
            {"role": "user", "content": "Grade the agent's Excel output."},
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
        raise ValueError(f"Excel content grading failed: {str(e)}") from e


def _find_file_content(
    changes: dict[str, Any], file_path: str, tab_name: str | None
) -> str | None:
    """Find and extract content from a file in the diff changes."""
    # Search in created, modified, and unchanged files
    for change_type in ["created", "modified", "unchanged"]:
        for file_change in changes.get(change_type, []):
            if file_change.get("path") == file_path:
                # For Excel files with multiple sheets
                if tab_name and file_change.get("sub_artifact_changes"):
                    for sub in file_change["sub_artifact_changes"]:
                        if sub.get("title") == tab_name:
                            return sub.get("new_content") or sub.get("old_content")
                # Return full content if no specific tab or single-sheet
                return file_change.get("new_content") or file_change.get("old_content")

    return None
