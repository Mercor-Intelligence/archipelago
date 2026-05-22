"""
Virtual Coworker Agents (VCAs) are provisioned through Events. The Events API supports arbitrary events
triggered from programmatic and LLM checks, and is decoupled from annotation so
future VCA improvements do not jeopardize past annotation campaigns.

An EventDefinition has two components:
- Trigger: the condition that gates events
- Action: the dispatched result

Checkpoints are Coordinator-level implementation details. Each Checkpoint
checks all triggers globally. Actions are purposefully small: invoke VCA
or call tools.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, NonNegativeFloat, PositiveInt

# -------------------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------------------


class ToolCallSelector(BaseModel):
    tool_name: str | None = None
    actor_id: str | None = None


# -------------------------------------------------------------------------------------
# Event Triggers - Primitive
# -------------------------------------------------------------------------------------

ToolCallSeenEventTriggerType = Literal["tool_call_seen"]
ToolCallCountEventTriggerType = Literal["tool_call_count"]
PhysicalTimeElapsedEventTriggerType = Literal["physical_time_elapsed"]
PrimitiveEventTriggerType = (
    ToolCallSeenEventTriggerType
    | ToolCallCountEventTriggerType
    | PhysicalTimeElapsedEventTriggerType
)


class ToolCallSeenEventTrigger(BaseModel):
    type: ToolCallSeenEventTriggerType = "tool_call_seen"
    selector: ToolCallSelector = Field(default_factory=ToolCallSelector)


class ToolCallCountEventTrigger(BaseModel):
    type: ToolCallCountEventTriggerType = "tool_call_count"
    selector: ToolCallSelector = Field(default_factory=ToolCallSelector)
    count: PositiveInt


class PhysicalTimeElapsedEventTrigger(BaseModel):
    type: PhysicalTimeElapsedEventTriggerType = "physical_time_elapsed"
    after_seconds: NonNegativeFloat


PrimitiveEventTrigger = Annotated[
    ToolCallSeenEventTrigger
    | ToolCallCountEventTrigger
    | PhysicalTimeElapsedEventTrigger,
    Field(discriminator="type"),
]


# -------------------------------------------------------------------------------------
# Event Triggers - Expression
# -------------------------------------------------------------------------------------

AndEventTriggerType = Literal["and"]
OrEventTriggerType = Literal["or"]
EventTriggerType = PrimitiveEventTriggerType | AndEventTriggerType | OrEventTriggerType


class AndEventTrigger(BaseModel):
    type: AndEventTriggerType = "and"
    triggers: list[EventTrigger] = Field(min_length=1)


class OrEventTrigger(BaseModel):
    type: OrEventTriggerType = "or"
    triggers: list[EventTrigger] = Field(min_length=1)


EventTrigger = Annotated[
    PrimitiveEventTrigger | AndEventTrigger | OrEventTrigger,
    Field(discriminator="type"),
]

AndEventTrigger.model_rebuild()
OrEventTrigger.model_rebuild()


# -------------------------------------------------------------------------------------
# Event Actions
# -------------------------------------------------------------------------------------

InvokeAgentActionType = Literal["invoke_agent"]
CallMCPToolActionType = Literal["call_mcp_tool"]
EventActionType = InvokeAgentActionType | CallMCPToolActionType


class InvokeAgentAction(BaseModel):
    type: InvokeAgentActionType = "invoke_agent"
    action_id: str
    actor_id: str
    timeout_seconds: PositiveInt | None = None


class CallMCPToolAction(BaseModel):
    type: CallMCPToolActionType = "call_mcp_tool"
    action_id: str
    actor_id: str  # TA, VCA, or Coordinator
    tool_name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


EventAction = Annotated[
    InvokeAgentAction | CallMCPToolAction,
    Field(discriminator="type"),
]


# -------------------------------------------------------------------------------------
# Event Definitions
# -------------------------------------------------------------------------------------


class EventDefinition(BaseModel):
    event_id: str
    enabled: bool = True
    trigger: EventTrigger
    actions: list[EventAction] = Field(default_factory=list)
