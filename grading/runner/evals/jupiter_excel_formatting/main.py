"""Excel formatting grading - 25% weight for formatting checks."""

import json
from typing import Any

from litellm import Choices

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.llm import call_llm

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

EXCEL_FORMATTING_SYSTEM_PROMPT = """You are an expert Excel evaluator grading formatting.

FORMATTING CRITERION:
{criteria}

EXCEL CONTENT TO EVALUATE:
{excel_content}

TASK CONTEXT:
{prompt}

Evaluate whether the Excel file meets the formatting requirements.

Respond ONLY with valid JSON:
{{
  "grade": "PASS" or "FAIL",
  "explanation": "Brief explanation for the grade"
}}"""


async def excel_formatting_eval(input: EvalImplInput) -> VerifierResult:
    """Grade Excel formatting using LLM judge against custom criteria."""
    # Extract criteria
    criteria: str = input.verifier.verifier_values.get("criteria", "")
    agent_result_path: str = input.verifier.verifier_values.get("agent_result_path", "")
    tab_to_grade: str = input.verifier.verifier_values.get("tab_to_grade", "")

    if not criteria:
        raise ValueError("Missing required field: criteria")

    try:
        # Get diff_result from helpers
        if not input.helper_results:
            raise ValueError("Missing helper results")

        diff_result = input.helper_results[HelperIds.SNAPSHOT_DIFF]
        changes = diff_result.get("changes", {})

        # Find Excel file content
        excel_content = ""
        if agent_result_path:
            excel_content = (
                _find_file_content(changes, agent_result_path.lstrip("/"), tab_to_grade)
                or ""
            )

        # If no specific path, try to find any Excel file
        if not excel_content:
            for change_type in ["created", "modified"]:
                for file_change in changes.get(change_type, []):
                    path = file_change.get("path", "")
                    if path.endswith((".xlsx", ".xls", ".xlsm")):
                        excel_content = (
                            file_change.get("new_content")
                            or file_change.get("old_content")
                            or ""
                        )
                        break
                if excel_content:
                    break

        # Extract task prompt
        prompt = ""
        for msg in input.trajectory.messages:
            if msg.get("role") == "user" and msg.get("content"):
                prompt = msg.get("content") or ""
                break

        # Build user prompt
        system_prompt = EXCEL_FORMATTING_SYSTEM_PROMPT.format(
            criteria=criteria,
            excel_content=excel_content or "(No Excel content found)",
            prompt=prompt,
        )

        # Get model and extra args
        model: str = input.grading_settings.llm_judge_model
        extra_args: dict[str, Any] = input.grading_settings.llm_judge_extra_args or {}

        # Call LLM using utility function
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Grade the Excel formatting."},
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
        raise ValueError(f"Excel formatting grading failed: {str(e)}") from e


def _find_file_content(
    changes: dict[str, Any], file_path: str, tab_name: str | None
) -> str | None:
    """Find and extract content from a file in the diff changes."""
    for change_type in ["created", "modified", "unchanged"]:
        for file_change in changes.get(change_type, []):
            if file_change.get("path") == file_path:
                # For Excel files with multiple sheets
                if tab_name and file_change.get("sub_artifact_changes"):
                    for sub in file_change["sub_artifact_changes"]:
                        if sub.get("title") == tab_name:
                            return sub.get("new_content") or sub.get("old_content")
                # Return full content
                return file_change.get("new_content") or file_change.get("old_content")

    return None
