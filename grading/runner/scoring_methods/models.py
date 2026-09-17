from enum import StrEnum
from typing import Any

from pydantic import BaseModel

class ScoringMethodCategory(StrEnum):
    STANDARD = "Standard"
    CUSTOM = "Custom"

class ScoringMethodIds(StrEnum):
    TEMPLATE = "template"
    TASK_SCORE_UNWEIGHTED_AND_UNIVERSAL_PENALTY = (
        "task_score_unweighted_and_universal_penalty"
    )
    TASK_SCORE_AND_UNIVERSAL_PENALTY_WEIGHTED_SUM = (
        "task_score_and_universal_penalty_weighted_sum"
    )
    DEEP_RESEARCH_WEIGHTED_AVERAGE = "deep_research_weighted_average"
    # Same as DEEP_RESEARCH_WEIGHTED_AVERAGE, but applies score caps when expert
    # assessment criteria fall below a configurable pass-rate floor or when any
    # verifier flagged via a configurable Gate custom field fails.
    DEEP_RESEARCH_WEIGHTED_AVERAGE_WITH_GATES = (
        "deep_research_weighted_average_with_gates"
    )
    # Apex V1 Grade Score - simple pass/fail ratio scoring
    APEX_V1_GRADE_SCORE = "apex_v1_grade_score"
    CRITERIA_LEVEL_CLAMPED_AVERAGE = "criteria_level_clamped_average"
    # ACE Criterion Score - with hurdle logic for ACE grading
    ACE_CRITERION_SCORE = "ace_criterion_score"
    # Difficulty Weighted Average - reads `difficulty` (Low/Medium/High) from each
    # verifier and computes a weighted average. Weights are configurable
    # (defaults: Low=1, Medium=3, High=5).
    DIFFICULTY_WEIGHTED_AVERAGE = "difficulty_weighted_average"

class ScoringConfig(BaseModel):
    """
    Scoring config model for scoring-based evaluation system.
    """

    scoring_config_id: str
    scoring_config_name: str
    scoring_defn_id: str
    scoring_config_values: dict[str, Any]
