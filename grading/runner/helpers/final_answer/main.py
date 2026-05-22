"""Final answer helper - extracts agent's final answer."""

import io

from runner.models import AgentTrajectoryOutput


async def final_answer_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
) -> str:
    """
    Extract final answer from trajectory messages.

    Returns the last message's content. Works for all agent types:
    - ReAct Toolbelt: Last message is a tool response with the answer
    - Loop/Toolbelt/SingleShot: Last message is an assistant response with the answer
    """
    if trajectory.messages:
        last_msg = trajectory.messages[-1]
        content = last_msg.get("content")
        return str(content) if content else ""
    return ""
