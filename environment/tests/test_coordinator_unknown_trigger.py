"""
Forward-compatibility tests for EventTrigger.

An older image handed a trigger type it does not know must skip that event and
keep the rest of the config, so these validate configs written by a newer one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError

from runner.coordinator.checkpoints.check_occurrences import get_event_occurrences
from runner.coordinator.checkpoints.models import (
    CheckpointObservations,
    PhysicalTimeCheckpointObservation,
    ToolCallCheckpoint,
    ToolCallCheckpointObservation,
)
from runner.coordinator.config.models import CoordinatorConfig
from runner.coordinator.events.models import (
    _KNOWN_EVENT_TRIGGER_TYPES,
    AndEventTrigger,
    EventDefinition,
    OrEventTrigger,
    PhysicalTimeElapsedEventTrigger,
    ToolCallCountEventTrigger,
    ToolCallSeenEventTrigger,
    UnknownEventTrigger,
)

TARGET_ACTOR = "target_agent"

SEEN_TRIGGER: dict[str, JsonValue] = {
    "type": "tool_call_seen",
    "selector": {"tool_name": "slack_send_message"},
}
INVENTED_TRIGGER: dict[str, JsonValue] = {
    "type": "sim_time_elapsed_since",
    "after_seconds": 7200,
    "since": {"tool_name": "run_validation"},
}


def _observations() -> CheckpointObservations:
    return CheckpointObservations(
        tool_calls=[
            ToolCallCheckpointObservation(
                sequence=1,
                actor_id=TARGET_ACTOR,
                tool_name="slack_send_message",
                arguments={},
                result_summary=None,
                timestamp=datetime.now(UTC).isoformat(),
            )
        ],
        physical_time=PhysicalTimeCheckpointObservation(
            trajectory_started_at=datetime.now(UTC).isoformat()
        ),
    )


def _fired_event_ids(config: CoordinatorConfig) -> list[str]:
    occurrences = get_event_occurrences(
        events=config.events,
        checkpoint=ToolCallCheckpoint(),
        observations=_observations(),
        occurred_event_ids=set(),
    )
    return [occurrence.event.event_id for occurrence in occurrences]


def test_the_recognized_type_set_is_derived_not_empty() -> None:
    # An empty set would disable the guard below silently rather than loudly.
    assert _KNOWN_EVENT_TRIGGER_TYPES == {
        "tool_call_seen",
        "tool_call_count",
        "physical_time_elapsed",
        "and",
        "or",
    }


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (SEEN_TRIGGER, ToolCallSeenEventTrigger),
        (
            {"type": "tool_call_count", "count": 2, "selector": {"tool_name": "read"}},
            ToolCallCountEventTrigger,
        ),
        (
            {"type": "physical_time_elapsed", "after_seconds": 30},
            PhysicalTimeElapsedEventTrigger,
        ),
        ({"type": "and", "triggers": [SEEN_TRIGGER]}, AndEventTrigger),
        ({"type": "or", "triggers": [SEEN_TRIGGER]}, OrEventTrigger),
    ],
)
def test_a_readable_trigger_is_never_swallowed_by_the_unknown_arm(
    trigger: dict[str, JsonValue], expected: type
) -> None:
    event = EventDefinition.model_validate({"event_id": "e", "trigger": trigger})

    assert isinstance(event.trigger, expected)


def test_a_trigger_type_this_image_cannot_read_parses() -> None:
    event = EventDefinition.model_validate(
        {"event_id": "e", "trigger": INVENTED_TRIGGER}
    )

    assert isinstance(event.trigger, UnknownEventTrigger)
    assert event.trigger.type == "sim_time_elapsed_since"


def test_an_unreadable_trigger_nested_in_an_expression_parses() -> None:
    event = EventDefinition.model_validate(
        {
            "event_id": "e",
            "trigger": {"type": "or", "triggers": [SEEN_TRIGGER, INVENTED_TRIGGER]},
        }
    )

    assert isinstance(event.trigger, OrEventTrigger)
    assert [type(t) for t in event.trigger.triggers] == [
        ToolCallSeenEventTrigger,
        UnknownEventTrigger,
    ]


@pytest.mark.parametrize(
    "trigger",
    [
        {"type": "tool_call_count", "count": 0, "selector": {}},
        {"type": "tool_call_count", "selector": {}},
        {"type": "physical_time_elapsed", "after_seconds": -1},
        {"type": "and", "triggers": []},
    ],
)
def test_a_recognized_trigger_that_is_malformed_still_raises(
    trigger: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        EventDefinition.model_validate({"event_id": "e", "trigger": trigger})


def test_an_unreadable_trigger_never_fires() -> None:
    config = CoordinatorConfig.model_validate(
        {
            "enabled": True,
            "events": [{"event_id": "invented", "trigger": INVENTED_TRIGGER}],
        }
    )

    assert _fired_event_ids(config) == []


def test_one_unreadable_trigger_does_not_silence_the_rest_of_the_config() -> None:
    config = CoordinatorConfig.model_validate(
        {
            "enabled": True,
            "events": [
                {"event_id": "invented", "trigger": INVENTED_TRIGGER},
                {"event_id": "readable", "trigger": SEEN_TRIGGER},
            ],
        }
    )

    assert config.enabled
    assert _fired_event_ids(config) == ["readable"]


def test_an_unreadable_trigger_survives_a_json_round_trip() -> None:
    event = EventDefinition.model_validate(
        {"event_id": "e", "trigger": INVENTED_TRIGGER}
    )

    assert event.model_dump(mode="json")["trigger"] == INVENTED_TRIGGER
