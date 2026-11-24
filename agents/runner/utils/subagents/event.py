from typing import Any, Literal

from pydantic import BaseModel

from runner.agents.models import AgentStatus


class SubagentEvent(BaseModel):
    child_run_id: str
    sequence: int
    child_agent_config_id: str
    reason: Literal["wrapper_turn", "delegated_task"]
    parent_step: int | None
    input_message_count: int
    dropped_fields: tuple[str, ...]
    status: AgentStatus
    duration_s: float
    usage: dict[str, Any] | None
    final_answer: str | None
    error: str | None


def describe(event: SubagentEvent) -> str:
    return (
        f"Child run {event.sequence} ({event.child_agent_config_id}) "
        f"{event.status.value} in {event.duration_s:.1f}s"
    )
