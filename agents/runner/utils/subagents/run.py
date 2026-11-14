import asyncio
import time
from typing import Any, Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel

from runner.agents.models import (
    AgentImpl,
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
)

from .event import SubagentEvent, describe
from .run_input import with_child_trajectory_id


class ChildResult(BaseModel):
    output: AgentTrajectoryOutput
    event: SubagentEvent


def _log_event(event: SubagentEvent) -> None:
    logger.bind(message_type="subagent", **event.model_dump(mode="json")).info(
        describe(event)
    )


async def run_child(
    impl: AgentImpl,
    run_input: AgentRunInput,
    *,
    agent_config_id: str,
    reason: Literal["wrapper_turn", "delegated_task"],
    sequence: int,
    parent_step: int | None = None,
    dropped_fields: frozenset[str] = frozenset(),
    child_trajectory_id: str | None = None,
) -> ChildResult:
    """Awaits the child, tags its log lines with child_run_id, and writes one `subagent` log row."""
    child_input = with_child_trajectory_id(run_input, child_trajectory_id)
    child_run_id = (
        child_trajectory_id if child_trajectory_id is not None else uuid4().hex
    )
    started_at = time.monotonic()
    final_answers: list[str] = []

    def capture_final_answer(message: Any) -> None:
        final_answers.append(message.record["message"])

    def make_event(
        status: AgentStatus,
        *,
        usage: dict[str, Any] | None = None,
        final_answer: str | None = None,
        error: str | None = None,
    ) -> SubagentEvent:
        return SubagentEvent(
            child_run_id=child_run_id,
            sequence=sequence,
            child_agent_config_id=agent_config_id,
            reason=reason,
            parent_step=parent_step,
            input_message_count=len(child_input.initial_messages),
            dropped_fields=tuple(sorted(dropped_fields)),
            status=status,
            duration_s=time.monotonic() - started_at,
            usage=usage,
            final_answer=(
                final_answer
                if final_answer is not None
                else (final_answers[-1] if final_answers else None)
            ),
            error=error,
        )

    sink_id = logger.add(
        capture_final_answer,
        level="INFO",
        filter=lambda record: record["extra"].get("child_run_id") == child_run_id
        and record["extra"].get("message_type") == "final_answer"
        and not record["extra"].get("ephemeral"),
    )
    try:
        try:
            context = (
                logger.contextualize(
                    trajectory_id=child_trajectory_id, child_run_id=child_run_id
                )
                if child_trajectory_id is not None
                else logger.contextualize(child_run_id=child_run_id)
            )
            with context:
                output = await impl(child_input)
        except asyncio.CancelledError:
            event = make_event(AgentStatus.CANCELLED, error="cancelled")
            _log_event(event)
            raise
        except Exception as exc:
            event = make_event(
                AgentStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            _log_event(event)
            raise
    finally:
        logger.remove(sink_id)

    event = make_event(
        output.status,
        usage=output.usage,
        final_answer=output.final_answer,
        error=output.error,
    )
    _log_event(event)
    return ChildResult(output=output, event=event)
