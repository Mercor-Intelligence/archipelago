import io

from runner.models import AgentTrajectoryOutput


async def template_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
):
    return {
        "template_result": "template_result",
    }
