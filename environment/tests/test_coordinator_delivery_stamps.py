"""
Matcher-level tests for ToolCallSelector.conditions.

No app returns a delivery stamp yet, so these build the stamped tool result by
hand and drive get_event_occurrences with it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastmcp.tools import ToolResult
from pydantic import JsonValue

from runner.coordinator.checkpoints.check_occurrences import get_event_occurrences
from runner.coordinator.checkpoints.delivery_stamps import (
    DELIVERY_INCOMPLETE_RESULT_KEY,
    DELIVERY_STAMPS_RESULT_KEY,
    DeliveryResolution,
    DeliveryStamp,
    read_observed_delivery,
)
from runner.coordinator.checkpoints.models import (
    CheckpointObservations,
    PhysicalTimeCheckpointObservation,
    ToolCallCheckpoint,
    ToolCallCheckpointObservation,
)
from runner.coordinator.events.models import (
    ChannelCondition,
    DeliveryStampCondition,
    EventDefinition,
    ToolCallArgumentCondition,
    ToolCallCondition,
    ToolCallSeenEventTrigger,
    ToolCallSelector,
    UnknownToolCallCondition,
)
from runner.coordinator.utils import (
    HAS_STRUCTURED_CONTENT_SUMMARY_KEY,
    MAX_STRUCTURED_CONTENT_BYTES,
    STRUCTURED_CONTENT_SUMMARY_KEY,
    summarize_tool_result,
)

TARGET_ACTOR = "target_agent"


def make_tool_call(
    *,
    tool_name: str = "slack_send_message",
    arguments: dict[str, JsonValue] | None = None,
    result_summary: dict[str, JsonValue] | None = None,
) -> ToolCallCheckpointObservation:
    return ToolCallCheckpointObservation(
        sequence=1,
        actor_id=TARGET_ACTOR,
        tool_name=tool_name,
        arguments=arguments if arguments is not None else {},
        result_summary=result_summary,
        timestamp=datetime.now(UTC).isoformat(),
    )


def stamped(*stamps: JsonValue) -> dict[str, JsonValue]:
    """A result summary shaped the way summarize_tool_result writes one."""
    return {
        "content_items": 1,
        HAS_STRUCTURED_CONTENT_SUMMARY_KEY: True,
        STRUCTURED_CONTENT_SUMMARY_KEY: {DELIVERY_STAMPS_RESULT_KEY: list(stamps)},
    }


def matched(
    selector: ToolCallSelector, tool_call: ToolCallCheckpointObservation
) -> bool:
    observations = CheckpointObservations(
        tool_calls=[tool_call],
        physical_time=PhysicalTimeCheckpointObservation(
            trajectory_started_at=datetime.now(UTC).isoformat()
        ),
    )
    occurrences = get_event_occurrences(
        events=[
            EventDefinition(
                event_id="woke_the_vca",
                trigger=ToolCallSeenEventTrigger(selector=selector),
            )
        ],
        checkpoint=ToolCallCheckpoint(),
        observations=observations,
        occurred_event_ids=set(),
    )
    return bool(occurrences)


# -------------------------------------------------------------------------------------
# Parsing
# -------------------------------------------------------------------------------------


def test_unknown_condition_kind_parses_and_is_skipped() -> None:
    selector = ToolCallSelector.model_validate(
        {
            "tool_name": "send_message",
            "conditions": [{"kind": "invented_next_quarter", "payload": {"a": 1}}],
            "argument_conditions": [
                {"path": ["to"], "operator": "equals", "value": "bob"}
            ],
        }
    )
    stamped_call_to_bob = make_tool_call(
        arguments={"to": "bob"}, result_summary=stamped({"actor_ids": ["ann"]})
    )
    stamped_call_to_ann = make_tool_call(
        arguments={"to": "ann"}, result_summary=stamped({"actor_ids": ["ann"]})
    )

    assert len(selector.conditions) == 1
    # Nothing recognized left on the selector, so the argument conditions decide.
    assert matched(selector, stamped_call_to_bob)
    assert not matched(selector, stamped_call_to_ann)


def test_unknown_condition_kind_is_skipped_beside_a_recognized_one() -> None:
    selector = ToolCallSelector.model_validate(
        {
            "tool_name": "send_message",
            "conditions": [
                {"kind": "invented_next_quarter"},
                {"kind": "delivery_stamp", "actor_ids": ["bob"]},
            ],
        }
    )

    assert matched(
        selector, make_tool_call(result_summary=stamped({"actor_ids": ["bob"]}))
    )
    assert not matched(
        selector, make_tool_call(result_summary=stamped({"actor_ids": ["ann"]}))
    )


@pytest.mark.parametrize(
    "condition",
    [
        {},
        {"kind": 123},
        {"kind": None},
        {"kind": {"nested": [1, None]}},
        {"kind": "channel"},
        {"kind": "channel", "channel_key": None},
        {"kind": "delivery_stamp"},
        {"kind": "delivery_stamp", "actor_ids": "bob"},
        {"kind": "invented_next_quarter", "payload": {"a": [1, None]}},
    ],
)
def test_a_condition_this_image_cannot_read_parses_and_is_skipped(
    condition: dict[str, JsonValue],
) -> None:
    selector = ToolCallSelector.model_validate(
        {"tool_name": "send_message", "conditions": [condition]}
    )

    assert [type(c) for c in selector.conditions] == [UnknownToolCallCondition]
    # Skipped, so the call is judged on its argument conditions — of which there
    # are none, so every call by this tool name still matches.
    assert matched(selector, make_tool_call(result_summary=stamped({"actor_ids": []})))


@pytest.mark.parametrize("conditions", [[None], ["channel"], [[]], [1]])
def test_a_condition_that_is_not_even_an_object_parses_and_is_skipped(
    conditions: list[JsonValue],
) -> None:
    selector = ToolCallSelector.model_validate({"conditions": conditions})

    assert [type(c) for c in selector.conditions] == [UnknownToolCallCondition]


@pytest.mark.parametrize(
    "condition",
    [
        {"kind": "channel", "channel_key": "slack:C1"},
        {"kind": "delivery_stamp", "actor_ids": []},
        {"kind": "delivery_stamp", "actor_ids": ["bob"], "unexpected": 1},
    ],
)
def test_a_readable_condition_is_never_swallowed_by_the_unknown_arm(
    condition: dict[str, JsonValue],
) -> None:
    selector = ToolCallSelector.model_validate({"conditions": [condition]})

    assert [type(c) for c in selector.conditions] != [UnknownToolCallCondition]


def test_conditions_survive_a_json_round_trip() -> None:
    """An older image re-saving a newer image's config must not strip it."""
    written_by_a_newer_image: dict[str, JsonValue] = {
        "kind": "invented_next_quarter",
        "payload": {"a": [1, None]},
    }
    selector = ToolCallSelector.model_validate(
        {
            "tool_name": "send_message",
            "conditions": [
                {"kind": "channel", "channel_key": "slack:C1"},
                {"kind": "delivery_stamp", "actor_ids": ["bob"]},
                written_by_a_newer_image,
            ],
        }
    )

    dumped = json.loads(selector.model_dump_json())
    assert dumped["conditions"][2] == written_by_a_newer_image
    assert ToolCallSelector.model_validate(dumped) == selector


def test_malformed_delivery_stamp_is_unreadable_not_raised() -> None:
    # A dropped entry is a dropped recipient, so the survivors cannot stand in
    # for the whole set.
    tool_call = make_tool_call(
        result_summary=stamped("not-an-object", {"actor_ids": 7})
    )
    delivery = read_observed_delivery(tool_call)

    assert delivery.stamps == []
    assert delivery.resolution is DeliveryResolution.UNREADABLE


def test_a_call_that_returned_nothing_structured_is_unstamped() -> None:
    assert read_observed_delivery(make_tool_call()).resolution is (
        DeliveryResolution.UNSTAMPED
    )


def test_structured_content_without_stamps_is_unstamped_not_stamped() -> None:
    tool_call = make_tool_call(
        result_summary={
            HAS_STRUCTURED_CONTENT_SUMMARY_KEY: True,
            STRUCTURED_CONTENT_SUMMARY_KEY: {"message_id": "m_1"},
        }
    )
    delivery = read_observed_delivery(tool_call)

    # The app answered and said nothing about delivery, which is the same
    # standing as never having stamped at all.
    assert delivery.resolution is DeliveryResolution.UNSTAMPED
    assert delivery.stamps == []


def test_stamps_survive_the_real_tool_result_summarizer() -> None:
    result = ToolResult(
        content=[],
        structured_content={
            DELIVERY_STAMPS_RESULT_KEY: [
                {"channel_key": "slack:C1", "actor_ids": ["bob"]}
            ]
        },
    )
    tool_call = make_tool_call(result_summary=summarize_tool_result(result))

    assert read_observed_delivery(tool_call).stamps == [
        DeliveryStamp(channel_key="slack:C1", actor_ids=["bob"])
    ]


def test_structured_content_dropped_for_size_is_unreadable() -> None:
    oversized = ToolResult(
        content=[],
        structured_content={
            DELIVERY_STAMPS_RESULT_KEY: [
                {"channel_key": "slack:C1", "actor_ids": ["bob"]}
            ],
            "body": "x" * (MAX_STRUCTURED_CONTENT_BYTES + 1),
        },
    )
    tool_call = make_tool_call(result_summary=summarize_tool_result(oversized))

    assert tool_call.result_summary is not None
    assert tool_call.result_summary[HAS_STRUCTURED_CONTENT_SUMMARY_KEY] is True
    delivery = read_observed_delivery(tool_call)

    # Nor are they stamps that reached nobody — the summary dropped what the
    # app said, so the server cannot tell either way.
    assert delivery.resolution is DeliveryResolution.UNREADABLE
    assert delivery.stamps == []


# -------------------------------------------------------------------------------------
# Conditions
# -------------------------------------------------------------------------------------


def test_delivery_stamp_condition_matches_on_recipient_intersection() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["bob", "carol"])],
    )
    reached_bob = make_tool_call(
        result_summary=stamped({"channel_key": "slack:C1", "actor_ids": ["ann", "bob"]})
    )
    reached_nobody_we_want = make_tool_call(
        result_summary=stamped({"channel_key": "slack:C1", "actor_ids": ["ann"]})
    )

    assert matched(selector, reached_bob)
    assert not matched(selector, reached_nobody_we_want)


def test_delivery_stamp_condition_does_not_match_a_resolution_that_found_nobody() -> (
    None
):
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["bob"])],
        argument_conditions=[
            ToolCallArgumentCondition(path=["to"], operator="equals", value="bob")
        ],
    )
    reached_nobody = make_tool_call(
        arguments={"to": "bob"},
        result_summary=stamped({"channel_key": "slack:C1", "actor_ids": []}),
    )

    assert not matched(selector, reached_nobody)


def test_channel_condition_matches_by_equality() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[ChannelCondition(channel_key="slack:C1")],
    )

    assert matched(
        selector, make_tool_call(result_summary=stamped({"channel_key": "slack:C1"}))
    )
    assert not matched(
        selector, make_tool_call(result_summary=stamped({"channel_key": "slack:C2"}))
    )


def test_conditions_are_anded_together() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[
            ChannelCondition(channel_key="slack:C1"),
            DeliveryStampCondition(actor_ids=["bob"]),
        ],
    )
    right_channel_wrong_actor = make_tool_call(
        result_summary=stamped({"channel_key": "slack:C1", "actor_ids": ["ann"]})
    )
    both = make_tool_call(
        result_summary=stamped({"channel_key": "slack:C1", "actor_ids": ["bob"]})
    )

    assert not matched(selector, right_channel_wrong_actor)
    assert matched(selector, both)


def test_condition_matches_across_multiple_stamps_on_one_call() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["carol"])],
        argument_conditions=[
            ToolCallArgumentCondition(path=["to"], operator="equals", value="carol")
        ],
    )
    fanned_out = make_tool_call(
        arguments={"to": "the-whole-team"},
        result_summary=stamped(
            {"channel_key": "slack:C1", "actor_ids": ["ann"]},
            {"channel_key": "email", "actor_ids": ["carol"]},
        ),
    )

    assert matched(selector, fanned_out)


# -------------------------------------------------------------------------------------
# Precedence
# -------------------------------------------------------------------------------------


def test_stamp_decides_alone_over_argument_conditions() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["bob"])],
        argument_conditions=[
            ToolCallArgumentCondition(path=["to"], operator="equals", value="carol")
        ],
    )
    stamp_says_bob_arguments_say_ann = make_tool_call(
        arguments={"to": "ann"},
        result_summary=stamped({"actor_ids": ["bob"]}),
    )

    assert matched(selector, stamp_says_bob_arguments_say_ann)


def test_argument_conditions_still_decide_an_unstamped_call() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["bob"])],
        argument_conditions=[
            ToolCallArgumentCondition(path=["to"], operator="equals", value="bob")
        ],
    )

    assert matched(selector, make_tool_call(arguments={"to": "bob"}))
    assert not matched(selector, make_tool_call(arguments={"to": "ann"}))


def test_argument_conditions_are_unchanged_when_no_conditions_are_declared() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        argument_conditions=[
            ToolCallArgumentCondition(path=["to"], operator="equals", value="bob")
        ],
    )
    stamped_call_to_ann = make_tool_call(
        arguments={"to": "ann"}, result_summary=stamped({"actor_ids": ["bob"]})
    )

    assert not matched(selector, stamped_call_to_ann)


def test_empty_conditions_and_empty_argument_conditions_match_every_call() -> None:
    selector = ToolCallSelector(tool_name="send_message")

    assert matched(selector, make_tool_call())
    assert matched(selector, make_tool_call(result_summary=stamped({})))


def test_tool_name_and_actor_gates_still_win_over_a_matching_stamp() -> None:
    stamped_call = make_tool_call(result_summary=stamped({"actor_ids": ["bob"]}))
    conditions: list[ToolCallCondition] = [DeliveryStampCondition(actor_ids=["bob"])]

    assert not matched(
        ToolCallSelector(tool_name="read_file", conditions=conditions), stamped_call
    )
    assert not matched(
        ToolCallSelector(
            tool_name="send_message", actor_id="someone_else", conditions=conditions
        ),
        stamped_call,
    )


# -------------------------------------------------------------------------------------
# A resolution that reached nobody never falls back to arguments
# -------------------------------------------------------------------------------------


def bob_selector() -> ToolCallSelector:
    """Reads bob off the stamp, with an argument condition that would also match."""
    return ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["bob"])],
        argument_conditions=[
            ToolCallArgumentCondition(path=["to"], operator="equals", value="bob")
        ],
    )


def test_an_empty_stamp_list_does_not_fall_back_to_arguments() -> None:
    assert not matched(
        bob_selector(),
        make_tool_call(arguments={"to": "bob"}, result_summary=stamped()),
    )


def test_stamps_that_failed_to_parse_fall_back_to_arguments() -> None:
    # Unreadable is not nobody. The call still has to satisfy the arguments.
    assert matched(
        bob_selector(),
        make_tool_call(
            arguments={"to": "bob"}, result_summary=stamped("not-an-object", 7)
        ),
    )
    assert not matched(
        bob_selector(),
        make_tool_call(
            arguments={"to": "ann"}, result_summary=stamped("not-an-object", 7)
        ),
    )


def test_stamps_dropped_for_size_fall_back_to_arguments() -> None:
    oversized = ToolResult(
        content=[],
        structured_content={
            DELIVERY_STAMPS_RESULT_KEY: [{"actor_ids": ["bob"]}],
            "body": "x" * (MAX_STRUCTURED_CONTENT_BYTES + 1),
        },
    )

    # The send did reach bob; only our record of it was dropped for size. It
    # must not read as a delivery that reached nobody.
    assert matched(
        bob_selector(),
        make_tool_call(
            arguments={"to": "bob"}, result_summary=summarize_tool_result(oversized)
        ),
    )


def test_a_call_the_server_never_resolved_still_matches_on_arguments() -> None:
    assert matched(bob_selector(), make_tool_call(arguments={"to": "bob"}))
    assert not matched(bob_selector(), make_tool_call(arguments={"to": "ann"}))


def test_conditions_must_be_satisfied_by_one_and_the_same_delivery() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[
            ChannelCondition(channel_key="slack:C1"),
            DeliveryStampCondition(actor_ids=["bob"]),
        ],
    )
    split_across_two_deliveries = make_tool_call(
        result_summary=stamped(
            {"channel_key": "slack:C1", "actor_ids": ["ann"]},
            {"channel_key": "email", "actor_ids": ["bob"]},
        )
    )
    one_delivery_with_both = make_tool_call(
        result_summary=stamped(
            {"channel_key": "email", "actor_ids": ["ann"]},
            {"channel_key": "slack:C1", "actor_ids": ["bob"]},
        )
    )

    assert not matched(selector, split_across_two_deliveries)
    assert matched(selector, one_delivery_with_both)


# -------------------------------------------------------------------------------------
# A read that resolved some recipients and could not read others
# -------------------------------------------------------------------------------------


def partially_stamped(*stamps: JsonValue) -> dict[str, JsonValue]:
    """What a runner writes when it resolved these and could not read the rest."""
    return {
        "content_items": 1,
        HAS_STRUCTURED_CONTENT_SUMMARY_KEY: True,
        STRUCTURED_CONTENT_SUMMARY_KEY: {
            DELIVERY_STAMPS_RESULT_KEY: list(stamps),
            DELIVERY_INCOMPLETE_RESULT_KEY: True,
        },
    }


def test_an_incomplete_read_is_unreadable_but_keeps_its_stamps() -> None:
    delivery = read_observed_delivery(
        make_tool_call(
            result_summary=partially_stamped(
                {"channel_key": "acme-slack", "actor_ids": ["vca_riley"]}
            )
        )
    )
    assert delivery.resolution is DeliveryResolution.UNREADABLE
    assert [stamp.actor_ids for stamp in delivery.stamps] == [["vca_riley"]]


def test_a_partial_read_still_wakes_whoever_it_did_resolve() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["vca_riley"])],
        argument_conditions=[
            ToolCallArgumentCondition(
                path=["channel_id"], operator="equals", value="U0RILEY"
            )
        ],
    )
    assert matched(
        selector,
        make_tool_call(
            arguments={"channel_id": "D0001"},
            result_summary=partially_stamped(
                {"channel_key": "acme-slack", "actor_ids": ["vca_riley"]}
            ),
        ),
    )


def test_a_partial_read_that_names_someone_else_still_falls_back() -> None:
    """Sam's stamp does not answer for Riley, so the arguments get their turn --
    which is the whole point of admitting the read was incomplete."""
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["vca_riley"])],
        argument_conditions=[
            ToolCallArgumentCondition(
                path=["channel_id"], operator="equals", value="U0RILEY"
            )
        ],
    )
    assert matched(
        selector,
        make_tool_call(
            arguments={"channel_id": "U0RILEY"},
            result_summary=partially_stamped(
                {"channel_key": "acme-slack", "actor_ids": ["vca_sam"]}
            ),
        ),
    )
    assert not matched(
        selector,
        make_tool_call(
            arguments={"channel_id": "D0001"},
            result_summary=partially_stamped(
                {"channel_key": "acme-slack", "actor_ids": ["vca_sam"]}
            ),
        ),
    )


def test_a_complete_read_is_still_decided_by_its_stamps_alone() -> None:
    """Unchanged: without the incomplete marker a stamped call does not consult
    arguments, so a lane that legitimately reached nobody is not re-matched."""
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["vca_riley"])],
        argument_conditions=[
            ToolCallArgumentCondition(
                path=["channel_id"], operator="equals", value="U0RILEY"
            )
        ],
    )
    assert not matched(
        selector,
        make_tool_call(
            arguments={"channel_id": "U0RILEY"},
            result_summary=stamped(
                {"channel_key": "acme-slack", "actor_ids": ["vca_sam"]}
            ),
        ),
    )


def test_a_partial_read_with_nothing_to_fall_back_to_does_not_match_everything() -> (
    None
):
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["vca_riley"])],
    )
    assert not matched(
        selector,
        make_tool_call(
            arguments={"channel_id": "D0001"},
            result_summary=partially_stamped(
                {"channel_key": "acme-slack", "actor_ids": ["vca_sam"]}
            ),
        ),
    )
    # And the same selector still fires when the partial read does name Riley.
    assert matched(
        selector,
        make_tool_call(
            arguments={"channel_id": "D0001"},
            result_summary=partially_stamped(
                {"channel_key": "acme-slack", "actor_ids": ["vca_riley"]}
            ),
        ),
    )


def test_an_incomplete_marker_with_no_list_beside_it_is_unreadable() -> None:
    delivery = read_observed_delivery(
        make_tool_call(
            result_summary={
                "content_items": 1,
                HAS_STRUCTURED_CONTENT_SUMMARY_KEY: True,
                STRUCTURED_CONTENT_SUMMARY_KEY: {DELIVERY_INCOMPLETE_RESULT_KEY: True},
            }
        )
    )

    assert delivery.resolution is DeliveryResolution.UNREADABLE
    assert delivery.stamps == []


def test_an_unstamped_call_does_not_match_a_selector_with_nothing_else_to_check() -> (
    None
):
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["vca_riley"])],
    )

    assert not matched(selector, make_tool_call(arguments={"channel_id": "D0001"}))


def test_a_read_that_named_nobody_at_all_does_not_match_everything() -> None:
    selector = ToolCallSelector(
        tool_name="send_message",
        conditions=[DeliveryStampCondition(actor_ids=["vca_riley"])],
    )
    dropped: dict[str, JsonValue] = {
        "content_items": 1,
        HAS_STRUCTURED_CONTENT_SUMMARY_KEY: True,
    }
    for result_summary in (partially_stamped(), dropped):
        assert not matched(
            selector,
            make_tool_call(
                arguments={"channel_id": "D0001"}, result_summary=result_summary
            ),
        )
