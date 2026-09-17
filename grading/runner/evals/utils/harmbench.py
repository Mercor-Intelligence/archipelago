from __future__ import annotations

from runner.models import AgentStatus, AgentTrajectoryOutput
from runner.utils.trajectory import content_text, resolve_lazy_content


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
