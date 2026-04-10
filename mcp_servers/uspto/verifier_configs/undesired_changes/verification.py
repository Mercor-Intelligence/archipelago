import asyncio
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
from runner.utils.prompts import (
    UNIVERSAL_UNDESIRED_CHANGES_CRITERIA,
    UNIVERSAL_UNDESIRED_CHANGES_SYSTEM_PROMPT,
    UndesiredChangeSingleArtifactResponseSchema,
)
from runner.utils.scoring.constants import (
    TaskScoreAndUniversalPenaltyWeightedSumSettings,
    TaskScoreUnweightedAndUniversalPenaltySettings,
)
from runner.utils.snapshot_diff.main import extract_artifact_changes_from_diff
from runner.utils.snapshot_diff.types import ArtifactChange, ChangeType
from runner.utils.token_management import (
    count_tokens,
    get_model_context_limit,
    truncate_text_to_tokens,
)

from ...llm import call_llm
from ..decorators import register_verifier
from .config import UNDESIRED_CHANGES_CONFIG, UndesiredChangesInput


async def _grade_single_artifact_change(
    artifact_change: ArtifactChange,
    artifact_index: int,
    task_prompt: str,
    grading_config: "GradingConfig",
) -> tuple[bool, str, dict[str, Any], GradingPromptMetadata]:
    """Grade a single artifact change to determine if it's undesired.

    Returns:
        tuple containing:
        - is_undesired (bool): Whether the change is undesired
        - rationale (str): Explanation for the decision
        - usage_metrics (dict): Token usage metrics
        - prompt_metadata (GradingPromptMetadata): Full prompt information for this artifact
    """
    try:
        artifact_desc = _format_artifact_change_for_grading(
            artifact_change, grading_config.model
        )

        user_prompt = f"""# TASK PROMPT
{task_prompt}

# AGENT CHANGE TO EVALUATE
{artifact_desc}

# INSTRUCTIONS
{UNIVERSAL_UNDESIRED_CHANGES_CRITERIA}

Evaluate ONLY this specific change to determine if it's undesired."""

        logger.debug(
            f"[ARTIFACT {artifact_index + 1}] Grading {artifact_change.change_type.value} change: {artifact_change.path}"
        )

        raw_response, parsed_result, usage_metrics, messages = await call_llm(
            model=grading_config.model,
            system_prompt=UNIVERSAL_UNDESIRED_CHANGES_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format=UndesiredChangeSingleArtifactResponseSchema,
            extra_args=grading_config.extra_args,
        )

        is_undesired = parsed_result.get("is_undesired", False)
        rationale = parsed_result.get("rationale", "")

        logger.debug(
            f"[ARTIFACT {artifact_index + 1}] Result: {'UNDESIRED' if is_undesired else 'OK'} - {rationale[:100]}"
        )

        prompt_metadata = GradingPromptMetadata(
            system_prompt=UNIVERSAL_UNDESIRED_CHANGES_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            raw_llm_response=raw_response,
            parsed_result={
                "artifact_index": artifact_index,
                "artifact_path": artifact_change.path,
                "change_type": artifact_change.change_type.value,
                "is_undesired": is_undesired,
                "rationale": rationale,
            },
            messages=messages,
            prompt_type=f"undesired_changes_artifact_{artifact_index}",
            prompt_tokens=usage_metrics.get("prompt_tokens"),
            completion_tokens=usage_metrics.get("completion_tokens"),
            total_tokens=usage_metrics.get("prompt_tokens", 0)
            + usage_metrics.get("completion_tokens", 0),
            reasoning_tokens=usage_metrics.get("reasoning_tokens"),
            duration_seconds=None,
        )

        return is_undesired, rationale, usage_metrics, prompt_metadata

    except Exception as e:
        logger.error(
            f"[VERIFIER_GRADING_ERROR] UNDESIRED_CHANGES_LLM | "
            f"artifact_index={artifact_index + 1} | "
            f"artifact_path={artifact_change.path} | "
            f"change_type={artifact_change.change_type.value} | "
            f"error_type={type(e).__name__} | "
            f"error_message={str(e)}"
        )
        logger.exception(f"  Stack trace for artifact {artifact_change.path}:")

        error_metadata = GradingPromptMetadata(
            system_prompt=UNIVERSAL_UNDESIRED_CHANGES_SYSTEM_PROMPT,
            user_prompt=f"Error grading artifact {artifact_change.path}",
            raw_llm_response="",
            parsed_result={
                "artifact_index": artifact_index,
                "artifact_path": artifact_change.path,
                "change_type": artifact_change.change_type.value,
                "is_undesired": False,
                "rationale": f"Error evaluating: {str(e)}",
                "error": True,
            },
            messages=[],
            prompt_type=f"undesired_changes_artifact_{artifact_index}_error",
        )

        return False, f"Error evaluating: {str(e)}", {}, error_metadata


def _format_artifact_change_for_grading(
    artifact_change: ArtifactChange,
    model: str,
) -> str:
    """Format artifact change with smart token truncation (80% of model context)."""
    context_limit = get_model_context_limit(model)
    max_content_tokens = int(context_limit * 0.8)

    change_type = artifact_change.change_type.value.upper()
    path = artifact_change.path

    if artifact_change.index is not None:
        artifact_type = artifact_change.artifact_type.upper()
        title = artifact_change.title or f"{artifact_type} {artifact_change.index + 1}"
        header = f"[{change_type}] {path} :: {title} ({artifact_type} #{artifact_change.index})"
    else:
        header = f"[{change_type}] {path}"

    if artifact_change.change_type == ChangeType.CREATED and artifact_change.new_size:
        header += f" ({artifact_change.new_size} bytes)"
    elif artifact_change.change_type == ChangeType.DELETED and artifact_change.old_size:
        header += f" ({artifact_change.old_size} bytes)"
    elif artifact_change.change_type == ChangeType.MODIFIED:
        if artifact_change.old_size and artifact_change.new_size:
            header += (
                f" ({artifact_change.old_size} -> {artifact_change.new_size} bytes)"
            )

    parts = [header, ""]

    if artifact_change.change_type == ChangeType.CREATED:
        content = artifact_change.new_content or artifact_change.content_diff or ""
        if content:
            truncated_content = truncate_text_to_tokens(
                content, max_content_tokens, model
            )
            parts.append("Full content:")
            parts.append(truncated_content)

            original_tokens = count_tokens(content, model)
            if original_tokens > max_content_tokens:
                parts.append(
                    f"\n... (content truncated from {original_tokens} to {max_content_tokens} tokens)"
                )

    elif artifact_change.change_type == ChangeType.DELETED:
        content = artifact_change.old_content or ""
        if content:
            truncated_content = truncate_text_to_tokens(
                content, max_content_tokens, model
            )
            parts.append("Deleted content:")
            parts.append(truncated_content)

            original_tokens = count_tokens(content, model)
            if original_tokens > max_content_tokens:
                parts.append(
                    f"\n... (content truncated from {original_tokens} to {max_content_tokens} tokens)"
                )

    elif artifact_change.change_type == ChangeType.MODIFIED:
        if artifact_change.content_diff:
            truncated_diff = truncate_text_to_tokens(
                artifact_change.content_diff, max_content_tokens // 2, model
            )
            parts.append("Changes (diff):")
            parts.append(truncated_diff)

            original_tokens = count_tokens(artifact_change.content_diff, model)
            if original_tokens > max_content_tokens // 2:
                parts.append(
                    f"\n... (diff truncated from {original_tokens} to {max_content_tokens // 2} tokens)"
                )

        if artifact_change.new_content:
            truncated_new = truncate_text_to_tokens(
                artifact_change.new_content, max_content_tokens // 2, model
            )
            parts.append("\nFull new content:")
            parts.append(truncated_new)

            original_tokens = count_tokens(artifact_change.new_content, model)
            if original_tokens > max_content_tokens // 2:
                parts.append(
                    f"\n... (content truncated from {original_tokens} to {max_content_tokens // 2} tokens)"
                )

    return "\n".join(parts)


@register_verifier(config=UNDESIRED_CHANGES_CONFIG, input_type=UndesiredChangesInput)
async def grade_undesired_changes(
    verifier: "GradingVerifier",
    trajectory_id: str,
    task_prompt: str,
    final_answer: str,
    diff_result: dict[str, Any],
    grading_config: "GradingConfig",
    initial_snapshot_zip: zipfile.ZipFile | None = None,
    final_snapshot_zip: zipfile.ZipFile | None = None,
) -> GradeResult:
    try:
        logger.info("=" * 80)
        logger.info(
            "UNIVERSAL GRADING: Undesired Changes (Individual Artifact Grading)"
        )
        logger.info("=" * 80)

        artifact_changes = extract_artifact_changes_from_diff(diff_result)
        logger.info(
            f"Extracted {len(artifact_changes)} artifact changes to grade individually"
        )

        if not artifact_changes:
            logger.info("No artifact changes found - returning zero penalty")
            return GradeResult(
                trajectory_id=trajectory_id,
                verifier_id=verifier.verifier_id,
                verifier=verifier,
                grading_config=grading_config,
                judge_grade="deletions=0, edits=0, creations=0",
                score=0.0,
                judge_grade_rationale="No changes detected in snapshot diff",
                grading_prompts={},
            )

        logger.info(f"Grading {len(artifact_changes)} artifacts in parallel...")

        grading_tasks = [
            _grade_single_artifact_change(
                artifact_change=artifact_change,
                artifact_index=i,
                task_prompt=task_prompt,
                grading_config=grading_config,
            )
            for i, artifact_change in enumerate(artifact_changes)
        ]

        results = await asyncio.gather(*grading_tasks, return_exceptions=True)

        undesired_deletions = 0
        undesired_edits = 0
        undesired_creations = 0

        rationale_parts = []
        individual_prompts: list[GradingPromptMetadata] = []
        artifact_evaluations: list[dict[str, Any]] = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_reasoning_tokens = 0

        for i, (artifact_change, result) in enumerate(
            zip(artifact_changes, results, strict=True)
        ):
            if isinstance(result, Exception):
                logger.error(
                    f"[VERIFIER_GRADING_ERROR] UNDESIRED_CHANGES_ARTIFACT | "
                    f"artifact_index={i + 1} | "
                    f"artifact_path={artifact_change.path} | "
                    f"change_type={artifact_change.change_type.value} | "
                    f"error_type={type(result).__name__} | "
                    f"error_message={str(result)}"
                )
                error_message = f"Error - {str(result)}"
                rationale_parts.append(
                    f"[{artifact_change.change_type.value.upper()}] {artifact_change.path}: {error_message}"
                )

                artifact_evaluations.append(
                    {
                        "artifact_index": i,
                        "artifact_path": artifact_change.path,
                        "change_type": artifact_change.change_type.value,
                        "is_undesired": False,
                        "rationale": error_message,
                        "error": True,
                    }
                )
                continue

            is_undesired, rationale, usage_metrics, prompt_metadata = result  # pyright: ignore[reportGeneralTypeIssues]

            individual_prompts.append(prompt_metadata)

            path_display = artifact_change.path
            if artifact_change.index is not None:
                path_display += f" :: {artifact_change.title or f'{artifact_change.artifact_type} {artifact_change.index}'}"

            artifact_evaluations.append(
                {
                    "artifact_index": i,
                    "artifact_path": artifact_change.path,
                    "path_display": path_display,
                    "change_type": artifact_change.change_type.value,
                    "is_undesired": is_undesired,
                    "rationale": rationale,
                }
            )

            if is_undesired:
                if artifact_change.change_type == ChangeType.DELETED:
                    undesired_deletions += 1
                elif artifact_change.change_type == ChangeType.MODIFIED:
                    undesired_edits += 1
                elif artifact_change.change_type == ChangeType.CREATED:
                    undesired_creations += 1

                change_label = artifact_change.change_type.value.upper()
                rationale_parts.append(f"[{change_label}] {path_display}: {rationale}")

            total_prompt_tokens += usage_metrics.get("prompt_tokens", 0)
            total_completion_tokens += usage_metrics.get("completion_tokens", 0)
            total_reasoning_tokens += usage_metrics.get("reasoning_tokens", 0)

        if rationale_parts:
            final_rationale = "Undesired changes found:\n" + "\n".join(rationale_parts)
        else:
            final_rationale = "No undesired changes detected. All changes appear to be appropriate for the task requirements."

        logger.info(
            f"Undesired changes: {undesired_deletions} deletions, "
            f"{undesired_edits} edits, {undesired_creations} creations"
        )

        scoring_settings = grading_config.scoring_settings
        assert scoring_settings is not None
        typed_settings = cast(
            TaskScoreUnweightedAndUniversalPenaltySettings
            | TaskScoreAndUniversalPenaltyWeightedSumSettings,
            scoring_settings,
        )

        total_penalty = (
            (undesired_deletions * typed_settings.undesired_deletion_points)
            + (undesired_edits * typed_settings.undesired_edit_points)
            + (undesired_creations * typed_settings.undesired_creation_points)
        )

        capped_penalty = min(
            total_penalty, typed_settings.universal_undesired_changes_max_points
        )
        score = -capped_penalty
        grade_output = f"deletions={undesired_deletions}, edits={undesired_edits}, creations={undesired_creations}"

        logger.info(f"Penalty: {capped_penalty}/{total_penalty}, Score: {score}")
        logger.info(
            f"Total token usage: {total_prompt_tokens} prompt, {total_completion_tokens} completion"
        )

        extended_parsed_result = {
            "undesired_deletions": undesired_deletions,
            "undesired_edits": undesired_edits,
            "undesired_creations": undesired_creations,
            "rationale": final_rationale,
            "artifact_evaluations": artifact_evaluations,
            "total_penalty": total_penalty,
            "capped_penalty": capped_penalty,
            "deletion_points": typed_settings.undesired_deletion_points,
            "edit_points": typed_settings.undesired_edit_points,
            "creation_points": typed_settings.undesired_creation_points,
            "total_artifacts_graded": len(artifact_changes),
            "grading_method": "parallel_individual_artifacts",
        }

        grading_prompts = {
            "summary": GradingPromptMetadata(
                system_prompt=UNIVERSAL_UNDESIRED_CHANGES_SYSTEM_PROMPT,
                user_prompt=f"Graded {len(artifact_changes)} artifacts individually in parallel",
                raw_llm_response="",
                parsed_result=extended_parsed_result,
                messages=[],
                prompt_type="universal_undesired_changes_summary",
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                total_tokens=total_prompt_tokens + total_completion_tokens,
                reasoning_tokens=total_reasoning_tokens,
                duration_seconds=None,
            )
        }

        for idx, prompt_metadata in enumerate(individual_prompts):
            grading_prompts[f"artifact_{idx}"] = prompt_metadata

        return GradeResult(
            trajectory_id=trajectory_id,
            verifier_id=verifier.verifier_id,
            verifier=verifier,
            grading_config=grading_config,
            judge_grade=grade_output,
            score=float(score),
            judge_grade_rationale=final_rationale,
            grading_prompts=grading_prompts,
        )

    except Exception as e:
        logger.error(
            f"[VERIFIER_GRADING_ERROR] UNDESIRED_CHANGES_VERIFIER | "
            f"verifier_id={verifier.verifier_id} | "
            f"trajectory_id={trajectory_id} | "
            f"grading_run_id={grading_config.grading_run_id} | "
            f"error_type={type(e).__name__} | "
            f"error_message={str(e)}"
        )
        logger.exception(
            f"  Stack trace for undesired changes verifier {verifier.verifier_id}:"
        )
        return GradeResult(
            trajectory_id=trajectory_id,
            verifier_id=verifier.verifier_id,
            verifier=verifier,
            grading_config=grading_config,
            judge_grade="deletions=0, edits=0, creations=0",
            score=0.0,
            judge_grade_rationale="",
            status=GradeStatus.ERROR,
            message=str(e),
        )
