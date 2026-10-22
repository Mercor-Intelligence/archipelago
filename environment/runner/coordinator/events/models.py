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

from enum import StrEnum
from typing import Annotated, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeFloat,
    PositiveInt,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    model_validator,
)

# -------------------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------------------

# @apg_environment_event_models:start

ToolCallArgumentOperator = Literal["equals", "contains", "exists"]


class ToolCallArgumentCondition(BaseModel):
    path: list[str | int] = Field(min_length=1)
    operator: ToolCallArgumentOperator
    value: JsonValue | None = None


class ToolCallConditionKind(StrEnum):
    CHANNEL = "channel"
    DELIVERY_STAMP = "delivery_stamp"


class ChannelCondition(BaseModel):
    kind: Literal[ToolCallConditionKind.CHANNEL] = ToolCallConditionKind.CHANNEL
    channel_key: str


class DeliveryStampCondition(BaseModel):
    kind: Literal[ToolCallConditionKind.DELIVERY_STAMP] = (
        ToolCallConditionKind.DELIVERY_STAMP
    )
    # Required, so a condition carrying no fields at all cannot land here by
    # default and quietly narrow the selector to nobody.
    actor_ids: list[str]


class UnknownToolCallCondition(BaseModel):
    # Accepts any shape and keeps every field, so a condition written by a newer
    # image survives being read and re-saved by an older one.
    model_config = ConfigDict(extra="allow")

    kind: JsonValue = None


def _fall_back_to_unknown_condition(
    value: object, handler: ValidatorFunctionWrapHandler
) -> object:
    try:
        return handler(value)
    except ValidationError:
        return UnknownToolCallCondition.model_validate(
            value if isinstance(value, dict) else {}
        )


# Deliberately undiscriminated: a condition this image cannot read has to degrade
# to UnknownToolCallCondition and be skipped, because one validation error here
# would reject the whole config and every event in it.
ToolCallCondition = Annotated[
    ChannelCondition | DeliveryStampCondition | UnknownToolCallCondition,
    WrapValidator(_fall_back_to_unknown_condition),
]


class ToolCallSelector(BaseModel):
    tool_name: str | None = None
    actor_id: str | None = None
    argument_conditions: list[ToolCallArgumentCondition] = Field(default_factory=list)
    conditions: list[ToolCallCondition] = Field(default_factory=list)


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


_KNOWN_EVENT_TRIGGER_TYPES = frozenset(
    value for member in get_args(EventTriggerType) for value in get_args(member)
)


class UnknownEventTrigger(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Narrower than the condition equivalent's JsonValue: every consumer treats a
    # discriminator as a hashable string, and a non-string one is corruption
    # rather than a newer image.
    type: str | None = None

    @model_validator(mode="after")
    def _refuse_recognized_types(self) -> UnknownEventTrigger:
        # Without this the permissive shape above also accepts a *malformed*
        # known trigger, turning an authoring bug into an event that never fires.
        if self.type in _KNOWN_EVENT_TRIGGER_TYPES:
            raise ValueError(f"{self.type} is a recognized trigger type")
        return self


# Deliberately undiscriminated, as ToolCallCondition is: a trigger type this image
# cannot read has to degrade to UnknownEventTrigger and never match, because one
# validation error here rejects the whole config and silences every VCA in it.
EventTrigger = Annotated[
    PrimitiveEventTrigger | AndEventTrigger | OrEventTrigger | UnknownEventTrigger,
    Field(union_mode="left_to_right"),
]

AndEventTrigger.model_rebuild()
OrEventTrigger.model_rebuild()


# -------------------------------------------------------------------------------------
# Event Actions
# -------------------------------------------------------------------------------------

InvokeAgentActionType = Literal["invoke_agent"]
CallMCPToolActionType = Literal["call_mcp_tool"]
EventActionType = InvokeAgentActionType | CallMCPToolActionType


# "abort" stops the event at this action when it fails; "continue" runs the
# actions behind it anyway. Actions in an event are ordered but not declared
# dependent, so "abort" stays the default for anything that produces state a
# later action might read.
EventActionFailurePolicy = Literal["abort", "continue"]


class InvokeAgentAction(BaseModel):
    type: InvokeAgentActionType = "invoke_agent"
    action_id: str
    actor_id: str
    timeout_seconds: PositiveInt | None = None
    on_failure: EventActionFailurePolicy = "abort"


class CallMCPToolAction(BaseModel):
    type: CallMCPToolActionType = "call_mcp_tool"
    action_id: str
    actor_id: str  # TA, VCA, or Coordinator
    tool_name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    on_failure: EventActionFailurePolicy = "abort"


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


# @apg_environment_event_models:end
