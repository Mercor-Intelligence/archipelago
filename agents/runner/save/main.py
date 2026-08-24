"""
Save module for reporting trajectory results.
"""

from loguru import logger

from runner.agents.models import AgentTrajectoryOutput
from runner.utils.logging.final_answer import pop_final_answer

from .webhook import report_trajectory_result


def snapshot_id_from_output(output: AgentTrajectoryOutput, key: str) -> str | None:
    """A snapshot id an AGENT built itself, when the runner captured none.

    An agent that produces its own tree -- aide_code_agent's submission,
    lighthouse_code_agent's reconstructed diff pair -- uploads to S3 and reports
    the id on its output, so the runner forwards it over the ordinary webhook
    fields rather than each agent inventing a channel.

    A blank or non-string value reads as absent rather than being forwarded: an
    empty id reaching the webhook is worse than none, because the diff resolver
    would treat it as a snapshot that ought to exist.
    """
    value = (output.output or {}).get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


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
    # Denormalize the captured final answer onto the output so it rides the
    # completion webhook into `trajectories.final_answer` (RLS-9433). Prefer an
    # explicitly-set value; otherwise take the last sink-captured emission.
    if output.final_answer is None:
        output.final_answer = pop_final_answer(trajectory_id)

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
