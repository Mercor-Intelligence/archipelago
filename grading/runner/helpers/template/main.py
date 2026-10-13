from typing import IO

from runner.models import AgentTrajectoryOutput


async def template_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
):
    return {
        "template_result": "template_result",
    }
