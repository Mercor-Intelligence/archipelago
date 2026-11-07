from __future__ import annotations

from runner.models import AgentStatus, AgentTrajectoryOutput
from runner.utils.trajectory import content_text, resolve_lazy_content


def _ungradeable(trajectory: AgentTrajectoryOutput) -> str:
    """Return the reason this trajectory cannot be graded at all, else ""."""
    output = trajectory.output or {}
    raw_response = output.get("raw_response")
    choices = raw_response.get("choices") if isinstance(raw_response, dict) else None
    finish_reason = (
        choices[0].get("finish_reason")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    if any(
        isinstance(reason, str) and reason.lower() == "content_filter"
        for reason in (output.get("empty_answer_class"), finish_reason)
    ):
        return "target_content_filter"
    if trajectory.status != AgentStatus.COMPLETED:
        return "trajectory_not_completed"
    return ""


def harmbench_generations(
    trajectory: AgentTrajectoryOutput, *, scan_all_assistant_turns: bool
) -> tuple[list[tuple[int, str]], str]:
    """Return the assistant turns to grade as (index, text), or ([], reason).

    Single-shot methods grade the final turn. An iterative attack (PAIR/TAP)
    needs every turn: it succeeds if ANY turn elicited the behavior, and the
    harness appends the attacker's next turn after the last target reply, so the
    transcript ends on a user message and the final-turn read finds nothing.
    """
    reason = _ungradeable(trajectory)
    if reason:
        return [], reason
    messages = trajectory.messages
    if not messages:
        return [], "missing_generation"
    candidates = (
        [(i, m) for i, m in enumerate(messages) if m.get("role") == "assistant"]
        if scan_all_assistant_turns
        else (
            [(len(messages) - 1, messages[-1])]
            if messages[-1].get("role") == "assistant"
            else []
        )
    )
    generations: list[tuple[int, str]] = []
    for index, message in candidates:
        resolve_lazy_content(message)
        text = content_text(message.get("content"))
        if text.strip():
            generations.append((index, text))
    if not generations:
        return [], "missing_generation"
    return generations, ""


def harmbench_generation(trajectory: AgentTrajectoryOutput) -> tuple[str, str]:
    """Return the target model's final reply and an empty reason, or ("", reason) when it cannot be graded."""
    output = trajectory.output or {}
    raw_response = output.get("raw_response")
    choices = raw_response.get("choices") if isinstance(raw_response, dict) else None
    finish_reason = (
        choices[0].get("finish_reason")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    if any(
        isinstance(reason, str) and reason.lower() == "content_filter"
        for reason in (output.get("empty_answer_class"), finish_reason)
    ):
        return "", "target_content_filter"
    if trajectory.status != AgentStatus.COMPLETED:
        return "", "trajectory_not_completed"
    messages = trajectory.messages
    if not messages or messages[-1].get("role") != "assistant":
        return "", "missing_generation"
    resolve_lazy_content(messages[-1])
    generation = content_text(messages[-1].get("content"))
    if not generation.strip():
        return "", "missing_generation"
    return generation, ""
