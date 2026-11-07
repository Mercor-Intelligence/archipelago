from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict

from runner.agents.models import AgentStatus

from .run import ChildResult


class SubagentResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    subagent_id: str
    child_trajectory_id: str
    spawn_ref: str | None
    depth: int
    status: AgentStatus
    final_answer: str | None
    error: str | None
    fault: dict[str, Any] | None
    usage: dict[str, Any] | None
    duration_s: float

    @classmethod
    def from_child(
        cls,
        child: ChildResult,
        *,
        subagent_id: str,
        spawn_ref: str | None,
        depth: int,
    ) -> SubagentResult:
        return cls(
            subagent_id=subagent_id,
            child_trajectory_id=child.event.child_run_id,
            spawn_ref=spawn_ref,
            depth=depth,
            status=child.output.status,
            final_answer=child.event.final_answer,
            error=child.output.error,
            fault=deepcopy(child.output.fault),
            usage=deepcopy(child.output.usage),
            duration_s=child.event.duration_s,
        )

    def for_parent(self) -> str:
        if self.status != AgentStatus.COMPLETED:
            detail = f"error: {self.error or 'unknown'}"
        else:
            detail = (
                self.final_answer
                if self.final_answer is not None
                else f"error: {self.error or 'unknown'}"
            )
        return f"[subagent {self.subagent_id}] status={self.status.value}: {detail}"
