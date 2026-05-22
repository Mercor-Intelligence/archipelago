from collections.abc import Iterable
from datetime import datetime

from loguru import logger

from ...utils.tool_names import tool_name_matches
from ..events.models import (
    AndEventTrigger,
    EventDefinition,
    EventTrigger,
    OrEventTrigger,
    PhysicalTimeElapsedEventTrigger,
    ToolCallCountEventTrigger,
    ToolCallSeenEventTrigger,
    ToolCallSelector,
)
from .models import (
    AndEventTriggerOccurrence,
    Checkpoint,
    CheckpointObservations,
    EventOccurrence,
    EventTriggerOccurrence,
    OrEventTriggerOccurrence,
    PhysicalTimeElapsedEventTriggerOccurrence,
    ToolCallCheckpointObservation,
    ToolCallCountEventTriggerOccurrence,
    ToolCallSeenEventTriggerOccurrence,
)


def get_event_occurrences(
    events: Iterable[EventDefinition],
    checkpoint: Checkpoint,
    observations: CheckpointObservations,
    occurred_event_ids: set[str],
) -> list[EventOccurrence]:
    occurrences: list[EventOccurrence] = []
    for event in events:
        if not event.enabled or event.event_id in occurred_event_ids:
            continue
        trigger_occurrence = _check_triggers_for_event(event.trigger, observations)
        if trigger_occurrence is not None:
            logger.info(
                "Environment Coordinator event trigger matched "
                + f"event={event.event_id} checkpoint={checkpoint.type} "
                + f"trigger={trigger_occurrence.type}"
            )
            occurrences.append(
                EventOccurrence(
                    event=event,
                    status="running",
                    occurred_at=observations.observed_at.isoformat(),
                    checkpoint=checkpoint.type,
                    trigger=trigger_occurrence,
                )
            )
    return occurrences


def _check_triggers_for_event(
    trigger: EventTrigger, observations: CheckpointObservations
) -> EventTriggerOccurrence | None:
    if isinstance(trigger, ToolCallSeenEventTrigger):
        return _check_tool_call_seen_event_trigger(trigger, observations)
    if isinstance(trigger, ToolCallCountEventTrigger):
        return _check_tool_call_count_event_trigger(trigger, observations)
    if isinstance(trigger, PhysicalTimeElapsedEventTrigger):
        return _check_physical_time_elapsed_event_trigger(trigger, observations)
    if isinstance(trigger, AndEventTrigger):
        return _check_and_event_trigger(trigger, observations)
    if isinstance(trigger, OrEventTrigger):
        return _check_or_event_trigger(trigger, observations)
    raise ValueError(f"Unknown EventTrigger type: {trigger.type}")


def _check_tool_call_seen_event_trigger(
    trigger: ToolCallSeenEventTrigger, observations: CheckpointObservations
) -> ToolCallSeenEventTriggerOccurrence | None:
    for tool_call in reversed(observations.tool_calls):
        if _tool_call_matches_selector(tool_call, trigger.selector):
            logger.info(
                "Environment Coordinator tool_call_seen trigger matched "
                + f"sequence={tool_call.sequence} actor={tool_call.actor_id} "
                + f"tool={tool_call.tool_name}"
            )
            return ToolCallSeenEventTriggerOccurrence(tool_call=tool_call)
    return None


def _check_tool_call_count_event_trigger(
    trigger: ToolCallCountEventTrigger, observations: CheckpointObservations
) -> ToolCallCountEventTriggerOccurrence | None:
    observed_calls = [
        tool_call
        for tool_call in observations.tool_calls
        if _tool_call_matches_selector(tool_call, trigger.selector)
    ]
    if len(observed_calls) < trigger.count:
        return None
    logger.info(
        "Environment Coordinator tool_call_count trigger matched "
        + f"count={len(observed_calls)} required={trigger.count} "
        + f"last_sequence={observed_calls[-1].sequence} "
        + f"last_actor={observed_calls[-1].actor_id} "
        + f"last_tool={observed_calls[-1].tool_name}"
    )
    return ToolCallCountEventTriggerOccurrence(
        observed_tool_call_count=len(observed_calls),
        last_call=observed_calls[-1],
    )


def _tool_call_matches_selector(
    tool_call: ToolCallCheckpointObservation, selector: ToolCallSelector
) -> bool:
    if selector.actor_id is not None and tool_call.actor_id != selector.actor_id:
        return False
    return tool_name_matches(
        configured_tool_name=selector.tool_name,
        observed_tool_name=tool_call.tool_name,
    )


def _check_physical_time_elapsed_event_trigger(
    trigger: PhysicalTimeElapsedEventTrigger, observations: CheckpointObservations
) -> PhysicalTimeElapsedEventTriggerOccurrence | None:
    started_at = datetime.fromisoformat(
        observations.physical_time.trajectory_started_at
    )
    elapsed = (observations.observed_at - started_at).total_seconds()
    if elapsed < trigger.after_seconds:
        return None
    return PhysicalTimeElapsedEventTriggerOccurrence(
        trajectory_started_at=observations.physical_time.trajectory_started_at,
        elapsed_seconds=elapsed,
    )


def _check_and_event_trigger(
    trigger: AndEventTrigger, observations: CheckpointObservations
) -> AndEventTriggerOccurrence | None:
    trigger_occurrences: list[EventTriggerOccurrence] = []
    for child_trigger in trigger.triggers:
        child_occurrence = _check_triggers_for_event(child_trigger, observations)
        if child_occurrence is None:
            return None
        trigger_occurrences.append(child_occurrence)
    return AndEventTriggerOccurrence(triggers=trigger_occurrences)


def _check_or_event_trigger(
    trigger: OrEventTrigger, observations: CheckpointObservations
) -> OrEventTriggerOccurrence | None:
    trigger_occurrences: list[EventTriggerOccurrence] = []
    for child_trigger in trigger.triggers:
        child_occurrence = _check_triggers_for_event(child_trigger, observations)
        if child_occurrence is not None:
            trigger_occurrences.append(child_occurrence)
    if not trigger_occurrences:
        return None
    return OrEventTriggerOccurrence(triggers=trigger_occurrences)
