"""Pydantic models for the PPTX style verifier."""

from pydantic import BaseModel


class StyleCriterion(BaseModel):
    """A single style criterion to evaluate."""

    criteria_name: str
    criteria_prompt: str
    judge_mode: str  # "text" | "image"
    criteria_scope: str  # "deck" | "slide"
    criteria_tag: str  # "style" | "output_correctness" | "work_product"
    judge_prompt: str = ""


class CriterionResult(BaseModel):
    """Result for a single criterion from the LLM judge."""

    score: float  # 0, 1, or NaN (stored as -1 internally)
    rationale: str
