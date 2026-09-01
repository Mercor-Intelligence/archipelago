"""Typed models for trace verifier configs and results."""

from typing import Any, TypedDict


class BehaviorDef(TypedDict, total=False):
    """Behavior check definition from verifier config."""

    check_type: str
    name: str
    threshold: int
    window_seconds: float
    penalty_points: float


class MilestoneStep(TypedDict, total=False):
    """Single milestone step in a milestone definition."""

    order: int
    action_type: str
    selector: str
    url_pattern: str
    text_content: str


class MilestoneDef(TypedDict, total=False):
    """Milestone check definition from verifier config."""

    name: str
    milestones: list[dict[str, Any] | MilestoneStep]


class Violation(TypedDict, total=False):
    """Violation record from behavior detection."""

    window_start: float
    window_end: float
    action_count: int
    threshold: int
    action_signature: str
    repetition_count: int
    start_index: int
    end_index: int
    consecutive_errors: int
    errors: list[str]


class EvalResult(TypedDict):
    """Result from evaluate_behavior or evaluate_milestone."""

    score: float
    verifier_result_values: dict[str, Any]
