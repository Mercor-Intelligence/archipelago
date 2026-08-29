"""Final answer helper - extracts agent's final answer."""

from typing import IO

from runner.models import AgentTrajectoryOutput
from runner.utils.trajectory import content_text, resolve_lazy_content


async def final_answer_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
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
            # Resolve first — without it str() yields "<ValidatorIterator ...>".
            resolve_lazy_content(msg)
            if msg.get("role") != "assistant":
                continue
            # Gate on the text the turn carries, not on content itself. Raw
            # truthiness cannot skip the turns this scan walks past: in block
            # form a tool-only turn holds no text block and an empty turn holds
            # one whose text is "", and both are truthy. Flattening with
            # _content_to_str would instead resurrect a falsy [] as "[]".
            # content_text renders all three as "".
            text = content_text(msg.get("content"))
            if text:
                return text
        return ""

    resolve_lazy_content(messages[-1])
    # content_text on this path too. There is no earlier turn to fall back to
    # here, but the hazard is the same one: a turn holding only non-text blocks
    # is truthy, and flattening it yields a repr of the blocks rather than an
    # answer. It also subsumes the empty-content guard, since content_text of
    # "", None and [] is "".
    return content_text(messages[-1].get("content"))
