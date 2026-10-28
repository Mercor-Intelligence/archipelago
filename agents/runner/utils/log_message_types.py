"""Compare the log message types a run emitted against its harness's declaration."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import loguru
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from runner.agents.models import AgentStatus

FINAL_ANSWER = "final_answer"
# Emitted by shared runner/utils code under any harness, so no declaration owns them.
RUNNER_LOG_MESSAGE_TYPES = frozenset(
    {
        "compaction",
        "context",
        "intermediate_output",
        "llm_retry_summary",
        "mcp_session",
        "resum",
        "sandbox_files_staged",
        "secondary_credentials",
    }
)


class LogTypeCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    missing_final_answer: bool
    undeclared: tuple[str, ...]


def check_log_message_types(
    declared: list[str], emitted: set[str], status: str
) -> LogTypeCheck:
    """Only a completed run can be missing its final answer."""
    return LogTypeCheck(
        missing_final_answer=status == AgentStatus.COMPLETED
        and FINAL_ANSWER not in emitted,
        undeclared=tuple(sorted(emitted - set(declared) - RUNNER_LOG_MESSAGE_TYPES)),
    )


class LogCapture(BaseModel):
    emitted: set[str] = Field(default_factory=set)
    agents: set[str] = Field(default_factory=set)


_CAPTURE: ContextVar[LogCapture | None] = ContextVar("log_capture", default=None)


def note_resolved_agent(agent_config_id: str) -> None:
    """Record an agent resolved while a capture is open, such as a wrapper's inner agent."""
    capture = _CAPTURE.get()
    if capture is not None:
        capture.agents.add(agent_config_id)


@contextmanager
def capture_log_message_types(trajectory_id: str) -> Iterator[LogCapture]:
    """Collect the INFO-and-above ``message_type`` values logged for one trajectory while open."""
    capture = LogCapture()

    def _matches(record: "loguru.Record") -> bool:
        extra = record["extra"]
        # Ephemeral records are re-emitted durably, as in the final_answer sink.
        return (
            extra.get("trajectory_id") == trajectory_id
            and isinstance(extra.get("message_type"), str)
            and not extra.get("ephemeral")
        )

    handler_id = logger.add(
        lambda message: capture.emitted.add(message.record["extra"]["message_type"]),
        level="INFO",  # durable sinks drop DEBUG
        filter=_matches,
    )
    token = _CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _CAPTURE.reset(token)
        logger.remove(handler_id)
