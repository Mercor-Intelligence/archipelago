import zipfile
from typing import Any

from loguru import logger

from runner.models import (
    GradeResult,
    GradeStatus,
    GradingConfig,
    GradingPromptMetadata,
    GradingVerifier,
)
from runner.utils.prompts import (
    UNIVERSAL_STYLE_CRITERIA,
    UNIVERSAL_STYLE_SYSTEM_PROMPT,
    UniversalStyleResponseSchema,
)
from runner.utils.screenshots import generate_screenshots_for_artifacts

from ...llm import call_llm
from ..decorators import register_verifier
from .config import STYLE_FORMATTING_CONFIG, StyleFormattingInput


@register_verifier(config=STYLE_FORMATTING_CONFIG, input_type=StyleFormattingInput)
async def grade_style_formatting(
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
        logger.info("UNIVERSAL GRADING: Style & Format")
        logger.info("=" * 80)

        # Generate screenshots for all created/modified artifacts
        visual_artifacts: list[dict[str, Any]] = []
        if final_snapshot_zip:
            artifacts_to_screenshot = []
            changes = diff_result.get("changes", {})

            for change_type in ["created", "modified"]:
                for artifact in changes.get(change_type, []):
                    artifacts_to_screenshot.append(
                        {
                            "path": artifact.get("path", ""),
                            "change_type": change_type,
                            "index": artifact.get("index"),
                            "artifact_type": artifact.get("artifact_type"),
                            "title": artifact.get("title"),
                        }
                    )

            if artifacts_to_screenshot:
                logger.info(
                    f"Generating screenshots for {len(artifacts_to_screenshot)} artifacts "
                    f"({len(changes.get('created', []))} created, {len(changes.get('modified', []))} modified)"
                )
                visual_artifacts = await generate_screenshots_for_artifacts(
                    zip_file=final_snapshot_zip,
                    artifacts=artifacts_to_screenshot,
                )

        if not visual_artifacts:
            logger.warning("No screenshots available for style evaluation")
            return GradeResult(
                trajectory_id=trajectory_id,
                verifier_id=verifier.verifier_id,
                verifier=verifier,
                grading_config=grading_config,
                judge_grade="pass",
                score=0.0,
                judge_grade_rationale="No visual artifacts to evaluate for style/formatting",
            )

        logger.info(
            f"Evaluating {len(visual_artifacts)} screenshots for style/formatting issues"
        )

        user_prompt = _build_universal_verifier_prompt(
            criteria=UNIVERSAL_STYLE_CRITERIA,
            visual_artifacts=visual_artifacts,
        )

        try:
            raw_response, parsed_result, usage_metrics, messages = await call_llm(
                model=grading_config.model,
                system_prompt=UNIVERSAL_STYLE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_format=UniversalStyleResponseSchema,
                extra_args=grading_config.extra_args,
                images=visual_artifacts if visual_artifacts else None,
            )

            is_no_issues = parsed_result.get("is_no_issues", False)
            grade_output = "pass" if is_no_issues else "fail"
            rationale = parsed_result.get("rationale", "")
        except Exception as e:
            logger.error(
                f"[VERIFIER_GRADING_ERROR] STYLE_FORMATTING_LLM | "
                f"verifier_id={verifier.verifier_id} | "
                f"error_type={type(e).__name__} | "
                f"error_message={str(e)}"
            )
            logger.exception("  Stack trace for style formatting LLM call:")
            raw_response = ""
            parsed_result = {}
            usage_metrics = {}
            messages = []
            grade_output = "fail"
            rationale = ""

            grading_prompts = {
                "grading": GradingPromptMetadata(
                    system_prompt=UNIVERSAL_STYLE_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    raw_llm_response=raw_response,
                    parsed_result=parsed_result,
                    messages=messages if messages else None,
                    visual_artifacts=visual_artifacts if visual_artifacts else None,
                    prompt_type="universal_style",
                )
            }

            return GradeResult(
                trajectory_id=trajectory_id,
                verifier_id=verifier.verifier_id,
                verifier=verifier,
                grading_config=grading_config,
                judge_grade=grade_output,
                score=0.0,
                judge_grade_rationale=rationale,
                grading_prompts=grading_prompts,
                status=GradeStatus.ERROR,
                message=f"LLM call failed: {str(e)}",
            )

        score = 0.0 if is_no_issues else -10.0
        logger.info(
            f"Style verification {'PASSED - no issues found' if is_no_issues else 'FAILED - style/formatting issues detected'} "
            f"(score: {score})"
        )

        grading_prompts = {
            "grading": GradingPromptMetadata(
                system_prompt=UNIVERSAL_STYLE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                raw_llm_response=raw_response,
                parsed_result=parsed_result,
                messages=messages,
                visual_artifacts=visual_artifacts if visual_artifacts else None,
                prompt_type="universal_style",
                prompt_tokens=usage_metrics.get("prompt_tokens"),
                completion_tokens=usage_metrics.get("completion_tokens"),
                total_tokens=usage_metrics.get("total_tokens"),
                reasoning_tokens=usage_metrics.get("reasoning_tokens"),
                duration_seconds=usage_metrics.get("duration_seconds"),
            )
        }

        return GradeResult(
            trajectory_id=trajectory_id,
            verifier_id=verifier.verifier_id,
            verifier=verifier,
            grading_config=grading_config,
            judge_grade=grade_output,
            score=score,
            judge_grade_rationale=rationale,
            grading_prompts=grading_prompts,
        )

    except Exception as e:
        logger.error(
            f"[VERIFIER_GRADING_ERROR] STYLE_FORMATTING_VERIFIER | "
            f"verifier_id={verifier.verifier_id} | "
            f"trajectory_id={trajectory_id} | "
            f"grading_run_id={grading_config.grading_run_id} | "
            f"error_type={type(e).__name__} | "
            f"error_message={str(e)}"
        )
        logger.exception(
            f"  Stack trace for style formatting verifier {verifier.verifier_id}:"
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


def _build_universal_verifier_prompt(
    criteria: str,
    visual_artifacts: list[dict[str, Any]],
) -> str:
    """Build prompt with screenshots and criteria."""
    screenshot_section = "VISUAL SCREENSHOTS OF CHANGED FILES:"
    for i, artifact in enumerate(visual_artifacts, 1):
        path = artifact.get("path", "")
        change_type = artifact.get("change_type", "unknown").upper()
        artifact_type = artifact.get("artifact_type")
        index = artifact.get("index")
        title = artifact.get("title")

        artifact_desc_parts = [f"`{path}`"]

        if artifact_type and artifact_type != "file":
            if index is not None:
                artifact_desc_parts.append(f"{artifact_type} {index}")
            else:
                artifact_desc_parts.append(artifact_type)

            if title:
                artifact_desc_parts.append(f'"{title}"')

        artifact_desc_parts.append(f"({change_type})")

        screenshot_section += f"\n[SCREENSHOT_{i}]: {' - '.join(artifact_desc_parts)}"

    prompt = f"""{screenshot_section}

# INSTRUCTIONS
{criteria}"""

    return prompt
