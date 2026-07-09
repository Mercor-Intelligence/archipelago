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
    post_populate_snapshot_id: str | None = None,
    env_image_layer_s3_uri: str | None = None,
):
    """
    Save trajectory results by reporting to RL Studio.

    In the new architecture, S3 snapshot upload is handled by the environment
    sandbox. This function just reports results via webhook.

    Args:
        trajectory_id: The trajectory ID
        output: The agent run output
        snapshot_id: The S3 snapshot ID (None if not created)
        post_populate_snapshot_id: S3 snapshot captured after populate hooks run (None if no hooks)
        env_image_layer_s3_uri: S3 URI of the captured rootfs layer (None if
            capture is disabled or failed); triggers the env image build
    """
    try:
        await report_trajectory_result(
            trajectory_id=trajectory_id,
            output=output,
            snapshot_id=snapshot_id,
            post_populate_snapshot_id=post_populate_snapshot_id,
            env_image_layer_s3_uri=env_image_layer_s3_uri,
        )
    except Exception as e:
        logger.error(f"Failed to report trajectory result: {repr(e)}")
        raise
