"""Base classes and shared types for the pluggable rubric eval."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel, Field

from runner.evals.models import EvalImplInput


def _derive_id_from_class_name(cls_name: str, strip_suffix: str = "") -> str:
    """Derive a stable snake_case id from a CamelCase class name."""
    name = cls_name.replace(strip_suffix, "") if strip_suffix else cls_name
    # Split acronym followed by capitalized word: ABCDef → ABC_Def
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    # Split lower/digit followed by upper: aB → a_B, 1A → 1_A
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()


class CriterionVerdict(BaseModel):
    """Structured judge output for a single criterion."""

    passed: bool = Field(description="True if the criterion is satisfied")
    reason: str = Field(description="Concise explanation citing evidence")


class BaseArtifactExtractor(ABC):
    """Fetch a piece of input for the judge from a trajectory or task snapshot."""

    id: str
    label: str = ""  # markdown header when this source is one of N composed inputs

    @abstractmethod
    async def fetch(self, input: EvalImplInput) -> str:
        """Return the text the judge will see for this input slot."""


class BaseJudgePrompt(ABC):
    """Build the prompt the judge sees for a single criterion.

    Subclasses don't need to declare `id` — it is derived from the class name
    by stripping 'Prompt' and converting to snake_case. Subclasses may still
    override `id` explicitly for irregular cases.
    """

    id: ClassVar[str]
    system_prompt: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "id" not in cls.__dict__:
            cls.id = _derive_id_from_class_name(cls.__name__, strip_suffix="Prompt")

    @abstractmethod
    def build_judge_prompt(
        self,
        *,
        criterion: str,
        rationale: str | None,
        category: list[str],
        agent_artifact: str,
        task_context: str = "",
        weight_label: str | None = None,
    ) -> str:
        """Return the user-prompt body the judge sees for this criterion."""
