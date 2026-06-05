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

    agent_in_playground captures are human-guided and tool-heavy: the transcript
    routinely ends on a tool-result message or a tool-only (empty) assistant
    turn, so ``messages[-1].content`` is either the raw tool output or empty —
    neither is the agent's answer. For AIP captures the answer is the agent's
    last *text* message, so we return that (skipping trailing tool/empty turns).
    """
    messages = trajectory.messages or []
    if not messages:
        return ""

    is_aip = (trajectory.output or {}).get("source") == "agent_in_playground"
    if is_aip:
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                return str(msg.get("content"))
        return ""

    content = messages[-1].get("content")
    return str(content) if content else ""
