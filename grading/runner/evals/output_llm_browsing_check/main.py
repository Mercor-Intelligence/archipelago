"""LLM Judge eval with browsing requirement classification.

Extends output_llm to additionally classify whether each criterion
requires internet browsing to verify against the prompt.
"""

import json
import zipfile

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from runner.evals.models import EvalImplInput
from runner.evals.output_llm.artifact_filters import is_valid_file_type
from runner.evals.output_llm.negative_criteria import (
    NEGATIVE_CRITERIA_ENABLED,
    evaluate_negative_criteria,
)
from runner.evals.output_llm.utils.log_helpers import (
    get_artifact_identity,
    log_artifact_selector_result,
    log_diff_extraction,
    log_grader_final_prompt,
    log_grader_result,
    log_grader_start,
    log_grader_truncation,
)
from runner.evals.output_llm.utils.prompts import (
    GRADING_SYSTEM_PROMPT,
    GRADING_SYSTEM_PROMPT_NO_REFERENCE,
    JSON_OUTPUT_GRADING,
    SECTION_SEPARATOR,
)
from runner.evals.output_llm.utils.services.artifact_evaluate import (
    select_artifacts_to_evaluate,
)
from runner.evals.output_llm.utils.services.artifact_reference import (
    ArtifactSelection,
    fetch_artifacts_to_reference,
)
from runner.evals.output_llm.utils.services.prompt_builder import build_grading_prompt
from runner.evals.output_llm.utils.shared import (
    LLM_JUDGE_TIMEOUT,
    MAX_JSON_RETRIES,
    estimate_artifact_tokens,
    extract_task_prompt,
    filter_artifacts_programmatically,
    get_artifact_display_names,
    should_auto_fail_missing_file_type,
)
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.models import VerifierResult
from runner.utils.llm import build_messages, call_llm
from runner.utils.token_utils import get_model_context_limit


class BrowsingCheckGradingResponseSchema(BaseModel):
    rationale: str = Field(description="Explanation of the assessment")
    is_criteria_true: bool = Field(description="Whether the criteria is met")
    requires_browsing: bool = Field(
        description="Whether verifying this criterion requires internet browsing"
    )
    browsing_rationale: str = Field(
        description="Explanation of why browsing is or isn't needed to verify this criterion"
    )


BROWSING_ASSESSMENT_PROMPT = """<BROWSING_ASSESSMENT>
Additionally, assess whether verifying this criterion requires real-time web search (e.g., checking a live URL, verifying real-time data, looking up a product page, confirming current prices or availability etc.)
Set it to true, if and only if browsing is required to verify the criterion against the model response in relation with the task prompt and static knowledge of the model or agent's output(model response) is not sufficient.
</BROWSING_ASSESSMENT>"""

BROWSING_JSON_OUTPUT = """<OUTPUT_FORMAT>
Respond with a JSON object:
{
  "rationale": #string,
  "is_criteria_true": #boolean,
  "requires_browsing": #boolean,
  "browsing_rationale": #string
}
- rationale: Your structured explanation following the RATIONALE_FORMAT above
- is_criteria_true: true if criterion is met, false if not
- requires_browsing: true if verifying this criterion would require browsing the internet
- browsing_rationale: Brief explanation of why browsing is or isn't needed
</OUTPUT_FORMAT>"""

_BASE_REMINDER = "- Return JSON with rationale and is_criteria_true"
_BROWSING_REMINDER = "- Return JSON with rationale, is_criteria_true, requires_browsing, and browsing_rationale"


def _build_browsing_system_prompt(base_prompt: str) -> str:
    """Replace the standard JSON output section with the extended one and add browsing instructions."""
    result = base_prompt.replace(
        JSON_OUTPUT_GRADING,
        BROWSING_ASSESSMENT_PROMPT + SECTION_SEPARATOR + BROWSING_JSON_OUTPUT,
    )
    if result == base_prompt:
        raise ValueError(
            "Failed to replace JSON_OUTPUT_GRADING in base prompt -- "
            "the constant may have changed without updating this module."
        )
    return result


BROWSING_GRADING_SYSTEM_PROMPT = _build_browsing_system_prompt(GRADING_SYSTEM_PROMPT)
BROWSING_GRADING_SYSTEM_PROMPT_NO_REFERENCE = _build_browsing_system_prompt(
    GRADING_SYSTEM_PROMPT_NO_REFERENCE
)


async def llm_judge_browsing_check_eval(input: EvalImplInput) -> VerifierResult:
    """Grade agent output using LLM judge, with browsing requirement classification."""
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"
    criteria = verifier_values.get("criteria", "")

    negative_criteria = ""
    if NEGATIVE_CRITERIA_ENABLED:
        negative_criteria = (verifier_values.get("negative_criteria", "") or "").strip()

    log_grader_start(task_id, criteria, is_negative=False)

    if not criteria:
        raise ValueError("Missing required field: criteria")

    try:
        if not input.helper_results:
            raise ValueError("Missing helper results")

        final_answer = input.helper_results[HelperIds.FINAL_ANSWER]
        diff_result = input.helper_results[HelperIds.SNAPSHOT_DIFF]
        model = input.grading_settings.llm_judge_model
        extra_args = input.grading_settings.llm_judge_extra_args
        task_prompt = extract_task_prompt(input)
        all_artifacts = extract_artifact_changes_from_diff(diff_result)

        log_diff_extraction(task_id, diff_result, all_artifacts, criteria=criteria)

        expected_file_type = verifier_values.get("expected_file_type")
        if not expected_file_type:
            expected_file_type = (
                "All output (modified files and final message in console)"
            )
        elif not is_valid_file_type(expected_file_type):
            expected_file_type = (
                "All output (modified files and final message in console)"
            )

        filtered_artifacts = filter_artifacts_programmatically(
            all_artifacts,
            expected_file_type,
            task_id=task_id,
            criteria=criteria,
        )

        if should_auto_fail_missing_file_type(expected_file_type, filtered_artifacts):
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                verifier_result_values={
                    "judge_grade": "fail",
                    "grade_rationale": (
                        f"No files matching the expected type ({expected_file_type}) were found. "
                        f"The agent did not produce any artifacts of the required type."
                    ),
                    "evaluated_artifacts": "",
                    "requires_browsing": False,
                    "browsing_rationale": "Auto-failed due to missing file type; browsing assessment not applicable.",
                    "auto_failed": True,
                    "auto_fail_reason": "no_matching_file_type",
                },
            )

        total_artifact_tokens = sum(
            estimate_artifact_tokens(a, model) for a in filtered_artifacts
        )
        context_limit = get_model_context_limit(model)
        artifact_budget_threshold = int(context_limit * 0.50)

        if total_artifact_tokens <= artifact_budget_threshold:
            selected_artifacts = filtered_artifacts
            selection_metadata = None
        else:
            selected_artifacts, selection_metadata = await select_artifacts_to_evaluate(
                filtered_artifacts,
                criteria,
                model=model,
                extra_args=extra_args,
                task_id=task_id,
                task_prompt=task_prompt,
            )

        selected_identities = {get_artifact_identity(a) for a in selected_artifacts}
        rejected_artifacts = [
            a
            for a in filtered_artifacts
            if get_artifact_identity(a) not in selected_identities
        ]

        log_artifact_selector_result(
            task_id,
            input_count=len(filtered_artifacts),
            selected_count=len(selected_artifacts),
            selected_artifacts=selected_artifacts,
            criteria=criteria,
            rejected_artifacts=rejected_artifacts if rejected_artifacts else None,
        )

        artifacts_to_reference_specs = verifier_values.get("artifacts_to_reference", [])
        artifacts_to_reference = None

        if artifacts_to_reference_specs:
            parsed_specs = [
                ArtifactSelection(**spec) if isinstance(spec, dict) else spec
                for spec in artifacts_to_reference_specs
            ]
            input.initial_snapshot_bytes.seek(0)
            with zipfile.ZipFile(input.initial_snapshot_bytes, "r") as initial_zip:
                artifacts_to_reference = await fetch_artifacts_to_reference(
                    artifacts_to_reference=parsed_specs,
                    initial_snapshot_zip=initial_zip,
                    task_id=task_id,
                    criteria=criteria,
                )
            input.initial_snapshot_bytes.seek(0)

        constructed_prompt = build_grading_prompt(
            criteria=criteria,
            final_answer=final_answer,
            model=model,
            artifacts_to_evaluate=selected_artifacts if selected_artifacts else None,
            artifacts_to_reference=artifacts_to_reference,
            include_full_content=True,
            task_id=task_id,
            expected_file_type=expected_file_type,
            task_prompt=task_prompt,
        )

        constructed_prompt.user_prompt = constructed_prompt.user_prompt.replace(
            _BASE_REMINDER, _BROWSING_REMINDER
        )

        if constructed_prompt.token_metadata:
            log_grader_truncation(
                task_id,
                was_truncated=constructed_prompt.token_metadata.get(
                    "was_truncated", False
                ),
                original_tokens=constructed_prompt.token_metadata.get(
                    "total_original_tokens", 0
                ),
                final_tokens=constructed_prompt.token_metadata.get(
                    "total_final_tokens", 0
                ),
                files_metadata=constructed_prompt.token_metadata.get("files"),
                criteria=criteria,
            )

        system_prompt = (
            BROWSING_GRADING_SYSTEM_PROMPT
            if artifacts_to_reference
            else BROWSING_GRADING_SYSTEM_PROMPT_NO_REFERENCE
        )

        log_grader_final_prompt(
            task_id=task_id,
            criteria=criteria,
            is_negative=False,
            model=model,
            system_prompt_chars=len(system_prompt),
            user_prompt_chars=len(constructed_prompt.user_prompt),
            artifacts_to_evaluate=selected_artifacts if selected_artifacts else None,
            artifacts_to_reference=artifacts_to_reference,
            image_count=len(constructed_prompt.visual_artifacts_to_evaluate or []),
        )

        messages = build_messages(
            system_prompt=system_prompt,
            user_prompt=constructed_prompt.user_prompt,
            images=constructed_prompt.visual_artifacts_to_evaluate,
        )

        parsed = None
        raw_content = None
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
                logger.warning(
                    f"[JUDGE] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: empty response"
                )
                continue

            raw_content = choices[0].message.content
            if not raw_content:
                logger.warning(
                    f"[JUDGE] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: empty content"
                )
                continue

            try:
                try:
                    raw_json = json.loads(raw_content)
                    if isinstance(raw_json, dict) and isinstance(
                        raw_json.get("rationale"), dict
                    ):
                        raw_json["rationale"] = json.dumps(raw_json["rationale"])
                        raw_content = json.dumps(raw_json)
                except json.JSONDecodeError:
                    pass

                parsed = BrowsingCheckGradingResponseSchema.model_validate_json(
                    raw_content
                )
                break
            except ValidationError as e:
                logger.warning(
                    f"[JUDGE] JSON retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
                )
                continue

        if parsed is None:
            raise ValueError(f"Invalid JSON after {MAX_JSON_RETRIES} attempts")

        is_criteria_true = parsed.is_criteria_true
        rationale = parsed.rationale
        judge_grade = "pass" if is_criteria_true else "fail"
        evaluated_artifact_names = get_artifact_display_names(selected_artifacts)

        result_values = {
            "judge_grade": judge_grade,
            "grade_rationale": rationale,
            "evaluated_artifacts": evaluated_artifact_names,
            "requires_browsing": parsed.requires_browsing,
            "browsing_rationale": parsed.browsing_rationale,
        }

        log_grader_result(
            task_id,
            is_negative=False,
            passed=is_criteria_true,
            score=1.0 if is_criteria_true else 0.0,
            criteria=criteria,
        )

        if is_criteria_true:
            score = 1.0
        elif negative_criteria:
            score = await evaluate_negative_criteria(
                task_id=task_id,
                negative_criteria=negative_criteria,
                all_artifacts=all_artifacts,
                expected_file_type=expected_file_type,
                final_answer=final_answer,
                model=model,
                extra_args=extra_args,
                task_prompt=task_prompt,
                artifacts_to_reference=artifacts_to_reference,
                artifact_budget_threshold=artifact_budget_threshold,
                result_values=result_values,
                filter_artifacts_fn=filter_artifacts_programmatically,
                estimate_tokens_fn=estimate_artifact_tokens,
            )
        else:
            score = 0.0

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values=result_values,
        )

    except Exception as e:
        error_msg = f"LLM grading failed: {str(e)}"
        raise ValueError(error_msg) from e
