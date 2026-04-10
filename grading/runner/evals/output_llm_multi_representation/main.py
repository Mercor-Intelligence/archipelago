import json
import zipfile

from litellm import Choices
from loguru import logger
from pydantic import ValidationError

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.helpers.snapshot_diff.types import Artifact
from runner.models import VerifierResult
from runner.utils.llm import build_messages, call_llm
from runner.utils.token_utils import get_model_context_limit

from ..output_llm.artifact_filters import is_valid_file_type
from ..output_llm.utils.log_helpers import (
    get_artifact_identity,
    log_artifact_selector_result,
    log_diff_extraction,
    log_grader_final_prompt,
    log_grader_result,
    log_grader_start,
    log_grader_truncation,
)
from ..output_llm.utils.prompts import (
    GRADING_SYSTEM_PROMPT,
    GRADING_SYSTEM_PROMPT_NO_REFERENCE,
    GradingResponseSchema,
)
from ..output_llm.utils.services.artifact_evaluate import (
    prepare_images_for_llm,
    select_artifacts_to_evaluate,
)
from ..output_llm.utils.services.prompt_builder import build_grading_prompt
from ..output_llm.utils.shared import (
    LLM_JUDGE_TIMEOUT,
    MAX_JSON_RETRIES,
    estimate_artifact_tokens,
    extract_task_prompt,
    filter_artifacts_programmatically,
    get_artifact_display_names,
    should_auto_fail_missing_file_type,
)
from .artifact_representations import (
    ArtifactSelectionWithTransformations,
    fetch_artifacts_with_transformations,
    transform_output_artifacts,
)


async def multi_representation_eval(input: EvalImplInput) -> VerifierResult:
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"
    criteria = verifier_values.get("criteria", "")

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

        # When enable_visual_grading is set, render output document files
        # (docx, pdf, pptx, xlsx) from the final snapshot as page images
        # so the LLM judge can evaluate visual properties like formatting,
        # colors, page count, and layout that are lost in text extraction.
        enable_visual_grading = verifier_values.get("enable_visual_grading", False)
        visual_output_artifacts: list[Artifact] = []
        if enable_visual_grading and selected_artifacts:
            input.final_snapshot_bytes.seek(0)
            with zipfile.ZipFile(input.final_snapshot_bytes, "r") as final_zip:
                visual_output_artifacts = await transform_output_artifacts(
                    selected_artifacts=selected_artifacts,
                    final_snapshot_zip=final_zip,
                    task_id=task_id,
                )
            input.final_snapshot_bytes.seek(0)

            if visual_output_artifacts:
                logger.info(
                    f"[JUDGE][GRADER] task={task_id} | generated {len(visual_output_artifacts)} "
                    f"visual representations of output artifacts"
                )

        # Fetch reference artifacts with representation expansion
        artifacts_to_reference_specs = verifier_values.get("artifacts_to_reference", [])
        artifacts_to_reference = None

        if artifacts_to_reference_specs:
            parsed_specs = [
                ArtifactSelectionWithTransformations(**spec)
                if isinstance(spec, dict)
                else spec
                for spec in artifacts_to_reference_specs
            ]

            input.initial_snapshot_bytes.seek(0)
            with zipfile.ZipFile(input.initial_snapshot_bytes, "r") as initial_zip:
                artifacts_to_reference = await fetch_artifacts_with_transformations(
                    artifacts_to_reference=parsed_specs,
                    initial_snapshot_zip=initial_zip,
                    task_id=task_id,
                    criteria=criteria,
                )
            input.initial_snapshot_bytes.seek(0)

            logger.info(
                f"[JUDGE][GRADER] task={task_id} | fetched {len(artifacts_to_reference)} "
                f"transformed artifacts from {len(artifacts_to_reference_specs)} specs"
            )

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

        # Merge rendered output artifact images into the prompt so the LLM
        # judge can see visual properties alongside the text extraction.
        if visual_output_artifacts:
            output_images = prepare_images_for_llm(visual_output_artifacts)
            if output_images:
                existing = constructed_prompt.visual_artifacts_to_evaluate or []
                constructed_prompt.visual_artifacts_to_evaluate = (
                    list(existing) + output_images
                )
                logger.info(
                    f"[JUDGE][GRADER] task={task_id} | added {len(output_images)} "
                    f"rendered output images to prompt "
                    f"(total images: {len(constructed_prompt.visual_artifacts_to_evaluate)})"
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
            GRADING_SYSTEM_PROMPT
            if artifacts_to_reference
            else GRADING_SYSTEM_PROMPT_NO_REFERENCE
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
        for _attempt in range(MAX_JSON_RETRIES):
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

            raw_content = choices[0].message.content
            if not raw_content:
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

                parsed = GradingResponseSchema.model_validate_json(raw_content)
                break
            except ValidationError:
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
        }

        log_grader_result(
            task_id,
            is_negative=False,
            passed=is_criteria_true,
            score=1.0 if is_criteria_true else 0.0,
            criteria=criteria,
        )

        score = 1.0 if is_criteria_true else 0.0

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values=result_values,
        )

    except Exception as e:
        error_msg = f"LLM grading failed: {str(e)}"
        raise ValueError(error_msg) from e
