from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue

from ..events.models import (
    AndEventTriggerType,
    EventActionType,
    EventDefinition,
    OrEventTriggerType,
    PhysicalTimeElapsedEventTriggerType,
    ToolCallCountEventTriggerType,
    ToolCallSeenEventTriggerType,
)

# -------------------------------------------------------------------------------------
# Checkpoints
# -------------------------------------------------------------------------------------

# @apg_environment_checkpoint_models:start


ToolCallCheckpointType = Literal["tool_call"]
PeriodicCheckpointType = Literal["periodic"]
CheckpointType = ToolCallCheckpointType | PeriodicCheckpointType


class ToolCallCheckpoint(BaseModel):
    type: ToolCallCheckpointType = "tool_call"


class PeriodicCheckpoint(BaseModel):
    type: PeriodicCheckpointType = "periodic"
    interval_seconds: float = Field(default=1.0, gt=0)


Checkpoint = Annotated[
    ToolCallCheckpoint | PeriodicCheckpoint,
    Field(discriminator="type"),
]


def default_checkpoints() -> list[Checkpoint]:
    return [ToolCallCheckpoint(), PeriodicCheckpoint()]


# -------------------------------------------------------------------------------------
# Checkpoint Observations
# -------------------------------------------------------------------------------------


class ToolCallCheckpointObservation(BaseModel):
    sequence: int
    actor_id: str  # TA, VCA, or Coordinator
    tool_name: str
    arguments: dict[str, JsonValue]
    result_summary: dict[str, JsonValue] | None = None
    error: str | None = None
    timestamp: str


class PhysicalTimeCheckpointObservation(BaseModel):
    trajectory_started_at: str


class CheckpointObservations(BaseModel):
    tool_calls: list[ToolCallCheckpointObservation] = Field(default_factory=list)
    physical_time: PhysicalTimeCheckpointObservation
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# -------------------------------------------------------------------------------------
# Event Occurrences - Primitive
# -------------------------------------------------------------------------------------


class ToolCallSeenEventTriggerOccurrence(BaseModel):
    type: ToolCallSeenEventTriggerType = "tool_call_seen"
    tool_call: ToolCallCheckpointObservation


class ToolCallCountEventTriggerOccurrence(BaseModel):
    type: ToolCallCountEventTriggerType = "tool_call_count"
    observed_tool_call_count: int
    last_call: ToolCallCheckpointObservation


class PhysicalTimeElapsedEventTriggerOccurrence(BaseModel):
    type: PhysicalTimeElapsedEventTriggerType = "physical_time_elapsed"
    trajectory_started_at: str
    elapsed_seconds: float


PrimitiveEventTriggerOccurrence = Annotated[
    ToolCallSeenEventTriggerOccurrence
    | ToolCallCountEventTriggerOccurrence
    | PhysicalTimeElapsedEventTriggerOccurrence,
    Field(discriminator="type"),
]


# -------------------------------------------------------------------------------------
# Event Occurrences - Expression
# -------------------------------------------------------------------------------------


class AndEventTriggerOccurrence(BaseModel):
    type: AndEventTriggerType = "and"
    triggers: list[EventTriggerOccurrence]


class OrEventTriggerOccurrence(BaseModel):
    type: OrEventTriggerType = "or"
    triggers: list[EventTriggerOccurrence]


EventTriggerOccurrence = Annotated[
    PrimitiveEventTriggerOccurrence
    | AndEventTriggerOccurrence
    | OrEventTriggerOccurrence,
    Field(discriminator="type"),
]

AndEventTriggerOccurrence.model_rebuild()
OrEventTriggerOccurrence.model_rebuild()


# -------------------------------------------------------------------------------------
# Event Occurrences
# -------------------------------------------------------------------------------------

EventOccurrenceStatus = Literal["running", "completed", "failed"]
ActionDispatchStatus = Literal["completed", "skipped", "failed"]


class ActionDispatch(BaseModel):
    action_id: str
    action_type: EventActionType
    status: ActionDispatchStatus
    started_at: str
    completed_at: str
    output: dict[str, JsonValue] | None = None
    error: str | None = None


class EventOccurrence(BaseModel):
    event: EventDefinition
    status: EventOccurrenceStatus
    occurred_at: str
    checkpoint: CheckpointType
    trigger: EventTriggerOccurrence
    dispatches: list[ActionDispatch] = Field(default_factory=list)


# @apg_environment_checkpoint_models:end
