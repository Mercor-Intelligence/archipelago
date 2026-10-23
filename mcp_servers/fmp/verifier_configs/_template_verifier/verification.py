"""
TODO: Implement your verification logic.
"""

import zipfile
from typing import Any, cast

from loguru import logger

from runner.models import GradeResult, GradeStatus, GradingConfig, GradingVerifier

from ..decorators import register_verifier
from ..validation import get_typed_config_input
from .config import YOUR_VERIFIER_CONFIG, YourVerifierInput


@register_verifier(config=YOUR_VERIFIER_CONFIG, input_type=YourVerifierInput)
async def grade_your_verifier_name(
    verifier: "GradingVerifier",
    trajectory_id: str,
    task_prompt: str,
    final_answer: str,
    diff_result: dict[str, Any],
    grading_config: "GradingConfig",
    initial_snapshot_zip: zipfile.ZipFile | None = None,
    final_snapshot_zip: zipfile.ZipFile | None = None,
) -> GradeResult:
    """
    TODO: Implement your verification logic.

    IMPORTANT: Error Handling
    - Wrap all logic in try/except
    - Return GradeResult with status=GradeStatus.ERROR and message="[your error message]" on failures
    - Errored grades are automatically excluded from scoring
    """
    # Get typed config input
    config_input = cast(YourVerifierInput, get_typed_config_input(verifier))
    logger.info(f"Config input: {config_input}")

    try:
        logger.info(f"Grading {verifier.verifier_id}")

        # TODO: Implement your grading logic here
        # Access config inputs like: config_input.example_criterion

        # If calling LLMs, let exceptions propagate to outer handler
        # The outer try/except will catch them and mark as errors

        # Example: Simple pass/fail based on some logic
        score = 1.0  # TODO: Calculate actual score
        grade = "pass"  # TODO: Determine pass/fail
        rationale = "TODO: Add rationale"

        return GradeResult(
            trajectory_id=trajectory_id,
            verifier_id=verifier.verifier_id,
            verifier=verifier,
            grading_config=grading_config,
            judge_grade=grade,
            score=score,
            judge_grade_rationale=rationale,
            # status defaults to GradeStatus.OK
        )

    except Exception as e:
        # Log the error for debugging
        logger.error(f"Failed to grade {verifier.verifier_id}: {e}")

        # Return error GradeResult - this will be:
        # 1. Saved to DB with status="error"
        # 2. Excluded from scoring calculations
        # 3. Displayed with error badge in UI
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
