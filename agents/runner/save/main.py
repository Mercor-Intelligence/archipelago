"""
Save module for reporting trajectory results.
"""

from loguru import logger

from runner.agents.models import AgentTrajectoryOutput

from .webhook import report_trajectory_result


async def save_results(
    trajectory_id: str,
    output: AgentTrajectoryOutput,
    snapshot_id: str | None,
):
    """
    Save trajectory results by reporting to RL Studio.

    In the new architecture, S3 snapshot upload is handled by the environment
    sandbox. This function just reports results via webhook.

    Args:
        trajectory_id: The trajectory ID
        output: The agent run output
        snapshot_id: The S3 snapshot ID (None if not created)
    """
    try:
        await report_trajectory_result(
            trajectory_id=trajectory_id,
            output=output,
            snapshot_id=snapshot_id,
        )
    except Exception as e:
        logger.error(f"Failed to report trajectory result: {repr(e)}")
        raise
