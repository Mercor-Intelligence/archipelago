from typing import Literal

from pydantic import BaseModel, SerializeAsAny, SkipValidation

from runner.agents.models import LitellmAnyMessage


class CompactionEvent(BaseModel):
    compaction_id: str
    strategy: str
    trigger: Literal["proactive", "overflow"]
    sequence: int
    history_index: int
    replaced_count: int
    kept_as_text_count: int
    tokens_before: int | None
    tokens_after: int | None
    summary: str
    injected: list[LitellmAnyMessage]
    step_range: tuple[int, int] | None = None


class CompactionResult(BaseModel):
    messages: SerializeAsAny[SkipValidation[list[LitellmAnyMessage]]]
    event: CompactionEvent | None = None
