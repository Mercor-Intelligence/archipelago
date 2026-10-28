from typing import Any

from loguru import logger

from runner.agents.models import LitellmAnyMessage, get_msg_content
from runner.utils.settings import get_settings

from .base import CompactionEvent, CompactionResult


def describe(event: CompactionEvent) -> str:
    if event.step_range is None:
        return "Compacted context"
    first, last = event.step_range
    return (
        f"Compacted step {first}"
        if first == last
        else f"Compacted steps {first}–{last}"
    )


class CompactionTranscript:
    def __init__(self) -> None:
        self._events: list[CompactionEvent] = []

    @property
    def event_count(self) -> int:
        return len(self._events)

    def log(self, event: CompactionEvent) -> None:
        logger.bind(
            message_type="compaction",
            compaction_id=event.compaction_id,
            strategy=event.strategy,
            trigger=event.trigger,
            sequence=event.sequence,
            step_range=event.step_range,
            replaced_count=event.replaced_count,
            kept_as_text_count=event.kept_as_text_count,
            tokens_before=event.tokens_before,
            tokens_after=event.tokens_after,
            summary=event.summary,
            payload="\n\n".join(
                content
                for message in event.injected
                if isinstance(content := get_msg_content(message), str)
            ),
        ).info(describe(event))

    def apply(self, result: CompactionResult) -> list[LitellmAnyMessage]:
        if result.event is not None:
            if get_settings().COMPACTION_TRANSCRIPT_ENABLED:
                self._events.append(result.event)
            self.log(result.event)
        return result.messages

    def saved_messages(
        self,
        system_msgs: list[LitellmAnyMessage],
        history: list[LitellmAnyMessage],
    ) -> list[LitellmAnyMessage]:
        messages = list(history)
        inserted_count = 0
        for event in self._events:
            insert_at = event.history_index + inserted_count
            messages[insert_at:insert_at] = event.injected
            inserted_count += len(event.injected)
        return [*system_msgs, *messages]

    def records(self, system_count: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        inserted_count = 0
        for event in self._events:
            record = event.model_dump(mode="json", exclude={"injected"})
            record["message_index"] = (
                system_count + event.history_index + inserted_count
            )
            records.append(record)
            inserted_count += len(event.injected)
        return records
