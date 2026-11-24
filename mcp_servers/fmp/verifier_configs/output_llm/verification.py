import zipfile
from typing import Any, cast

from loguru import logger

from runner.models import (
    GradeResult,
    GradeStatus,
    GradingConfig,
    GradingPromptMetadata,
    GradingVerifier,
)
from runner.utils.prompts import GRADING_SYSTEM_PROMPT, GradingResponseSchema
from runner.utils.snapshot_diff.main import extract_artifact_changes_from_diff
from runner.utils.snapshot_diff.types import Artifact

from ...llm import call_llm
from ...services.artifact_evaluate import select_artifacts_to_evaluate
from ...services.artifact_reference import fetch_artifacts_to_reference
from ...services.prompt_builder import build_grading_prompt
from ..decorators import register_verifier
from ..validation import get_typed_config_input
from .config import OUTPUT_LLM_CONFIG, OutputLLMInput


@register_verifier(config=OUTPUT_LLM_CONFIG, input_type=OutputLLMInput)
async def grade_output_llm(
    verifier: "GradingVerifier",
    trajectory_id: str,
    task_prompt: str,
    final_answer: str,
    diff_result: dict[str, Any],
    grading_config: "GradingConfig",
    initial_snapshot_zip: zipfile.ZipFile | None = None,
    final_snapshot_zip: zipfile.ZipFile | None = None,
) -> GradeResult:
    config_input = cast(OutputLLMInput, get_typed_config_input(verifier))

    try:
        logger.info(f"Grading OUTPUT verifier {verifier.verifier_id}")
        logger.debug(f"Final answer length: {len(final_answer)} characters")

        # Extract ArtifactChange objects TO EVALUATE from diff
        all_artifacts_to_evaluate = extract_artifact_changes_from_diff(diff_result)
        logger.info(
            f"Extracted {len(all_artifacts_to_evaluate)} artifact changes TO EVALUATE from diff"
        )

        # Initialize grading_prompts dict for storing all prompts/metadata
        grading_prompts_dict: dict[str, GradingPromptMetadata] = {}
        artifacts_to_evaluate_metadata = None

        reference_artifacts: list[Artifact] = []
        if config_input.artifacts_to_reference:
            try:
                reference_artifacts = await fetch_artifacts_to_reference(
                    artifacts_to_reference=config_input.artifacts_to_reference,
                    verifier_id=verifier.verifier_id,
                    initial_snapshot_zip=initial_snapshot_zip,
                )
                if reference_artifacts:
                    logger.info(
                        f"Successfully fetched {len(reference_artifacts)} reference artifacts"
                    )
            except Exception as e:
                logger.error(f"Failed to fetch artifacts TO REFERENCE: {e}")
                reference_artifacts = []

        # Select relevant artifacts to evaluate
        selected_artifacts_to_evaluate = []
        if all_artifacts_to_evaluate:
            logger.info(
                f"[PREPROCESSING] Selecting artifacts TO EVALUATE for criteria: {config_input.criteria}"
            )
            (
                selected_artifacts_to_evaluate,
                selection_metadata,
            ) = await select_artifacts_to_evaluate(
                all_artifacts_to_evaluate,
                config_input.criteria,
                model=grading_config.model,
                extra_args=grading_config.extra_args,
            )
            grading_prompts_dict["artifacts_to_evaluate_selection"] = (
                selection_metadata.to_metadata()
            )

        logger.info(
            f"[GRADING] Preparing grading with {len(selected_artifacts_to_evaluate)} artifacts to evaluate and {len(reference_artifacts)} reference artifacts"
        )

        # Build grading prompt
        constructed_prompt = build_grading_prompt(
            criteria=config_input.criteria,
            final_answer=final_answer,
            artifacts_to_evaluate=selected_artifacts_to_evaluate
            if selected_artifacts_to_evaluate
            else None,
            artifacts_to_reference=reference_artifacts if reference_artifacts else None,
            diff_result=diff_result,
            model=grading_config.model,
            include_full_content=True,
        )
        artifacts_to_evaluate_metadata = (
            constructed_prompt.artifacts_to_evaluate_metadata
        )

        # Grade the positive criterion
        grade_output, rationale, grading_metadata = await _grade_criterion(
            criteria=config_input.criteria,
            user_prompt=constructed_prompt.user_prompt,
            images=constructed_prompt.visual_artifacts_to_evaluate,
            model=grading_config.model,
            extra_args=grading_config.extra_args,
        )
        grading_prompts_dict["positive_grading"] = grading_metadata

        # Calculate score
        negative_grade_output = None
        negative_rationale = None

        if grade_output == "pass":
            score = 1.0
        else:
            if config_input.negative_criteria:
                neg_constructed_prompt = build_grading_prompt(
                    criteria=config_input.negative_criteria,
                    final_answer=final_answer,
                    artifacts_to_evaluate=selected_artifacts_to_evaluate
                    if selected_artifacts_to_evaluate
                    else None,
                    artifacts_to_reference=reference_artifacts
                    if reference_artifacts
                    else None,
                    diff_result=diff_result,
                    model=grading_config.model,
                    include_full_content=True,
                    is_negative=True,
                )

                neg_output, neg_rat, neg_metadata = await _grade_criterion(
                    criteria=config_input.negative_criteria,
                    user_prompt=neg_constructed_prompt.user_prompt,
                    images=neg_constructed_prompt.visual_artifacts_to_evaluate,
                    model=grading_config.model,
                    extra_args=grading_config.extra_args,
                )

                negative_grade_output = neg_output
                negative_rationale = neg_rat
                grading_prompts_dict["negative_grading"] = neg_metadata

                if neg_output == "pass":
                    score = -1.0
                else:
                    score = 0.0
            else:
                score = 0.0

        logger.info("─" * 80)
        logger.info(f"[GRADING COMPLETE] OUTPUT Verifier {verifier.verifier_id}")
        logger.info(f"  Grade: {grade_output}, Score: {score}")
        logger.info("─" * 80)

        return GradeResult(
            trajectory_id=trajectory_id,
            verifier_id=verifier.verifier_id,
            verifier=verifier,
            grading_config=grading_config,
            judge_grade=grade_output,
            score=score,
            judge_grade_rationale=rationale,
            judge_negative_grade=negative_grade_output,
            judge_negative_grade_rationale=negative_rationale,
            grading_prompts=grading_prompts_dict,
            artifacts_to_evaluate_metadata=artifacts_to_evaluate_metadata,
            # status defaults to GradeStatus.OK
        )

    except Exception as e:
        logger.error(
            f"[VERIFIER_GRADING_ERROR] OUTPUT_LLM_VERIFIER | "
            f"verifier_id={verifier.verifier_id} | "
            f"trajectory_id={trajectory_id} | "
            f"grading_run_id={grading_config.grading_run_id} | "
            f"error_type={type(e).__name__} | "
            f"error_message={str(e)}"
        )
        logger.exception(
            f"  Stack trace for output LLM verifier {verifier.verifier_id}:"
        )
        return GradeResult(
            trajectory_id=trajectory_id,
            verifier_id=verifier.verifier_id,
            verifier=verifier,
            grading_config=grading_config,
            judge_grade="fail",
            score=0.0,
            judge_grade_rationale="",
            status=GradeStatus.ERROR,
            message=str(e),
        )


async def _grade_criterion(
    criteria: str,
    user_prompt: str,
    images: list[dict[str, Any]] | None,
    model: str,
    extra_args: dict[str, Any] | None,
) -> tuple[str, str, GradingPromptMetadata]:
    """
    Internal helper to grade a single criterion (positive or negative).

    Args:
        criteria: The criterion to evaluate
        user_prompt: Complete user prompt (already formatted with all needed context)
        images: Optional list of all images for vision models
        model: Full model string (e.g., "openai/gpt-4o")
        extra_args: Extra arguments for the LLM

    Returns:
        Tuple of (grade_output, rationale, metadata)
    """
    logger.debug(f"Grading criterion (length: {len(criteria)} chars)")
    logger.debug(f"User prompt length: {len(user_prompt)} chars")
    if images:
        logger.debug(f"Including {len(images)} images for vision grading")

    # Use standard grading system prompt (includes strict requirements + tolerance notes)
    system_prompt = GRADING_SYSTEM_PROMPT

    # Call LLM with structured output - let errors propagate to top-level handler
    raw_response, parsed_result, usage_metrics, messages = await call_llm(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_format=GradingResponseSchema,
        extra_args=extra_args,
        images=images,
    )

    is_criteria_true = parsed_result.get("is_criteria_true", False)
    grade_output = "pass" if is_criteria_true else "fail"
    rationale = parsed_result.get("rationale", "")

    metadata = GradingPromptMetadata(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_llm_response=raw_response,
        parsed_result=parsed_result,
        messages=messages,
        visual_artifacts=images,
        prompt_type="grading",
        prompt_tokens=usage_metrics.get("prompt_tokens"),
        completion_tokens=usage_metrics.get("completion_tokens"),
        total_tokens=usage_metrics.get("total_tokens"),
        reasoning_tokens=usage_metrics.get("reasoning_tokens"),
        duration_seconds=usage_metrics.get("duration_seconds"),
    )

    return grade_output, rationale, metadata
