"""
A delivery stamp is the server's own record of a tool call: which channel it
landed on and which actors it actually reached. Apps return stamps in the tool
result's structured content; the coordinator reads them back off the recorded
observation so events can key on resolved recipients instead of argument text.
"""

from collections.abc import Iterable
from enum import StrEnum

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from ..events.models import (
    ChannelCondition,
    DeliveryStampCondition,
    ToolCallCondition,
)
from ..utils import (
    HAS_STRUCTURED_CONTENT_SUMMARY_KEY,
    STRUCTURED_CONTENT_SUMMARY_KEY,
)
from .models import ToolCallCheckpointObservation

DELIVERY_STAMPS_RESULT_KEY = "delivery_stamps"
# Set when a runner resolved some recipients and could not read others, so
# the stamps it wrote are a floor rather than the whole set.
DELIVERY_INCOMPLETE_RESULT_KEY = "delivery_incomplete"

RecognizedToolCallCondition = ChannelCondition | DeliveryStampCondition


class DeliveryStamp(BaseModel):
    channel_key: str | None = None
    actor_ids: list[str] = Field(default_factory=list)


class DeliveryResolution(StrEnum):
    """Whether the server knows who a call reached, which is three answers and
    not two. ``STAMPED`` with no stamps means it reached nobody; ``UNREADABLE``
    means we cannot say. Collapsing those reports a delivery that happened as
    one that reached nobody."""

    UNSTAMPED = "unstamped"
    STAMPED = "stamped"
    UNREADABLE = "unreadable"


class ObservedDelivery(BaseModel):
    resolution: DeliveryResolution = DeliveryResolution.UNSTAMPED
    stamps: list[DeliveryStamp] = Field(default_factory=list)


def read_observed_delivery(
    tool_call: ToolCallCheckpointObservation,
) -> ObservedDelivery:
    result_summary = tool_call.result_summary or {}
    if result_summary.get(HAS_STRUCTURED_CONTENT_SUMMARY_KEY) is not True:
        return ObservedDelivery()
    structured_content = result_summary.get(STRUCTURED_CONTENT_SUMMARY_KEY)
    if structured_content is None:
        # The app answered with structured content and the summary dropped it —
        # over MAX_STRUCTURED_CONTENT_BYTES, or not serializable. Whatever it
        # said is gone, which is not the same as it having said nobody.
        return ObservedDelivery(resolution=DeliveryResolution.UNREADABLE)
    if not isinstance(structured_content, dict):
        return ObservedDelivery()
    incomplete = structured_content.get(DELIVERY_INCOMPLETE_RESULT_KEY) is True
    raw_stamps = structured_content.get(DELIVERY_STAMPS_RESULT_KEY)
    if raw_stamps is None:
        # A runner that admits it could not read its recipients has told us
        # something, even with no list beside it: not that it reached nobody.
        if incomplete:
            return ObservedDelivery(resolution=DeliveryResolution.UNREADABLE)
        return ObservedDelivery()
    if not isinstance(raw_stamps, list):
        return ObservedDelivery(resolution=DeliveryResolution.UNREADABLE)
    stamps: list[DeliveryStamp] = []
    for raw_stamp in raw_stamps:
        try:
            stamps.append(DeliveryStamp.model_validate(raw_stamp))
        except ValidationError as error:
            logger.warning(
                "Environment Coordinator could not read delivery stamp "
                + f"sequence={tool_call.sequence} tool={tool_call.tool_name} "
                + f"error={error!r}"
            )
            # One dropped entry is one lost recipient, and the survivors cannot
            # be reported as the whole set.
            return ObservedDelivery(resolution=DeliveryResolution.UNREADABLE)
    if incomplete:
        # Readable and admittedly partial: the stamps stand for whoever they
        # name, and anyone else may still fall back to arguments.
        return ObservedDelivery(resolution=DeliveryResolution.UNREADABLE, stamps=stamps)
    return ObservedDelivery(resolution=DeliveryResolution.STAMPED, stamps=stamps)


def recognized_conditions(
    conditions: Iterable[ToolCallCondition],
) -> list[RecognizedToolCallCondition]:
    # A condition this image cannot read is skipped rather than refused, and a
    # selector left with none falls back to its argument conditions.
    return [
        condition
        for condition in conditions
        if isinstance(condition, ChannelCondition | DeliveryStampCondition)
    ]


def delivery_satisfies(
    conditions: Iterable[RecognizedToolCallCondition], delivery: ObservedDelivery
) -> bool:
    # One delivery has to satisfy every condition. Spreading them across separate
    # stamps would fire on a channel and an actor that never met.
    return any(
        all(_condition_matches(condition, stamp) for condition in conditions)
        for stamp in delivery.stamps
    )


def _condition_matches(
    condition: RecognizedToolCallCondition, stamp: DeliveryStamp
) -> bool:
    if isinstance(condition, ChannelCondition):
        return stamp.channel_key == condition.channel_key
    if isinstance(condition, DeliveryStampCondition):
        return bool(set(condition.actor_ids).intersection(stamp.actor_ids))
    raise ValueError(f"Unknown ToolCallCondition: {condition}")
