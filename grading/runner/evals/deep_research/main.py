"""Deep Research eval - LLM judge for evaluating research criteria."""

from typing import Any

from litellm import Choices
from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.models import VerifierResult
from runner.utils.llm import build_messages, call_llm

from ..output_llm.utils.prompts import GradingResponseSchema
from ..output_llm.utils.services.artifact_evaluate import select_artifacts_to_evaluate

# Default timeout for LLM judge calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

# System prompt for deep research grading
DEEP_RESEARCH_SYSTEM_PROMPT = """You are an expert evaluator assessing whether research output satisfies a given criterion.

You will be provided with:
1. A criterion description to evaluate
2. The agent's final answer/response
3. Optionally, relevant artifacts (files) produced by the agent

Your task is to determine if the criterion is satisfied based on the available evidence.

Respond with:
- is_criteria_true: true if the criterion is satisfied, false otherwise
- rationale: A clear explanation of your reasoning"""


def _extract_task_prompt(input: EvalImplInput) -> str | None:
    """Extract the task prompt from trajectory (first user message)."""
    if not input.trajectory or not input.trajectory.messages:
        return None

    for msg in input.trajectory.messages:
        if msg.get("role") == "user" and msg.get("content"):
            content = msg.get("content")
            return str(content) if content else None

    return None


def _build_deep_research_prompt(
    description: str,
    final_answer: str | None,
    artifacts: list[Any] | None,
    task_prompt: str | None = None,
) -> str:
    """Build the user prompt for deep research evaluation."""
    parts = []

    # Include task context if available
    if task_prompt:
        parts.append(f"## Task Given to Agent\n{task_prompt}")

    # The criterion to evaluate
    parts.append(f"## Criterion to Evaluate\n{description}")

    # Agent's final answer
    if final_answer:
        parts.append(f"## Agent's Final Answer\n{final_answer}")
    else:
        parts.append("## Agent's Final Answer\n(No final answer provided)")

    # Artifacts if any
    if artifacts:
        parts.append("## Relevant Artifacts")
        for artifact in artifacts:
            path = artifact.path
            # ArtifactChange uses new_content (full content) or content_diff (diff patch)
            content = artifact.new_content or artifact.content_diff
            if content:
                # Truncate very long content
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                parts.append(f"### {path}\n```\n{content}\n```")
            else:
                parts.append(f"### {path}\n(No content available)")
    else:
        parts.append("## Relevant Artifacts\n(No artifacts to evaluate)")

    # Final instruction
    parts.append(
        "## Evaluation\nBased on the above, determine if the criterion is satisfied."
    )

    return "\n\n".join(parts)


def _get_artifact_display_names(artifacts: list[Any]) -> str:
    """Build comma-separated string of artifact names."""
    if not artifacts:
        return ""

    names = []
    for artifact in artifacts:
        path = artifact.path
        artifact_type = getattr(artifact, "artifact_type", None)

        if artifact_type in ("slide", "sheet", "page") and artifact.index is not None:
            type_label = artifact_type.capitalize()
            index_display = artifact.index + 1
            title = getattr(artifact, "title", None)
            if title:
                names.append(f"{path} ({type_label} {index_display}: {title})")
            else:
                names.append(f"{path} ({type_label} {index_display})")
        else:
            names.append(path)

    return ", ".join(names)


async def deep_research_eval(input: EvalImplInput) -> VerifierResult:
    """
    Evaluate deep research criterion using LLM judge.

    Evaluates whether the agent's output satisfies the description criterion.
    Returns binary score: 1.0 (pass) or 0.0 (fail).
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    # 1. Get description (required)
    description = verifier_values.get("description", "")
    if not description:
        raise ValueError("Missing required field: description")

    logger.info(f"[DEEP_RESEARCH] task={task_id} | evaluating: {description[:100]}...")

    try:
        # 2. Get helper data
        if not input.helper_results:
            raise ValueError("Missing helper results")

        final_answer = input.helper_results.get(HelperIds.FINAL_ANSWER)
        diff_result = input.helper_results.get(HelperIds.SNAPSHOT_DIFF)

        # 3. Get model settings
        model = input.grading_settings.llm_judge_model
        extra_args = input.grading_settings.llm_judge_extra_args

        # 4. Extract task prompt from trajectory
        task_prompt = _extract_task_prompt(input)

        # 5. Extract artifacts from diff (no filtering - evaluate all)
        all_artifacts = []
        if diff_result:
            all_artifacts = extract_artifact_changes_from_diff(diff_result)

        logger.info(
            f"[DEEP_RESEARCH] task={task_id} | found {len(all_artifacts)} artifacts"
        )

        # 6. LLM artifact selection (if we have artifacts to select from)
        selected_artifacts = []
        if all_artifacts:
            selected_artifacts, _ = await select_artifacts_to_evaluate(
                all_artifacts,
                description,
                model=model,
                extra_args=extra_args,
                task_id=task_id,
                task_prompt=task_prompt,
            )

        logger.info(
            f"[DEEP_RESEARCH] task={task_id} | selected {len(selected_artifacts)} artifacts"
        )

        # 7. Build prompt
        user_prompt = _build_deep_research_prompt(
            description=description,
            final_answer=final_answer,
            artifacts=selected_artifacts if selected_artifacts else None,
            task_prompt=task_prompt,
        )

        # 8. Call LLM judge
        messages = build_messages(
            system_prompt=DEEP_RESEARCH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format=GradingResponseSchema,
        )

        # 9. Parse response
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")

        raw_content = choices[0].message.content
        if not raw_content:
            raise ValueError("LLM returned empty content")

        parsed = GradingResponseSchema.model_validate_json(raw_content)

        # 10. Build result
        passed = parsed.is_criteria_true
        score = 1.0 if passed else 0.0

        logger.info(
            f"[DEEP_RESEARCH] task={task_id} | result: {'PASS' if passed else 'FAIL'} | "
            f"criterion: {description[:50]}..."
        )

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "judge_grade": "pass" if passed else "fail",
                "grade_rationale": parsed.rationale,
                "evaluated_artifacts": _get_artifact_display_names(selected_artifacts),
            },
        )

    except Exception as e:
        error_msg = f"Deep research evaluation failed: {str(e)}"
        logger.error(f"[DEEP_RESEARCH] task={task_id} | error: {error_msg}")
        raise ValueError(error_msg) from e
