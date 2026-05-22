from pydantic import BaseModel, Field

from ..agents.models import VirtualCoworkerAgent
from ..checkpoints.models import (
    Checkpoint,
    default_checkpoints,
)
from ..events.models import (
    EventDefinition,
)


class CoordinatorConfig(BaseModel):
    enabled: bool = False
    agents: dict[str, VirtualCoworkerAgent] = Field(default_factory=dict)
    checkpoints: list[Checkpoint] = Field(default_factory=default_checkpoints)
    events: list[EventDefinition] = Field(default_factory=list)

    def model_dump_log_json(self) -> str:
        return self.model_dump_json(exclude={"agents": {"__all__": {"env"}}})
