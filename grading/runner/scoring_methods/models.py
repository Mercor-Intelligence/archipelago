from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ScoringMethodCategory(StrEnum):
    STANDARD = "Standard"
    CUSTOM = "Custom"


# @apg_scoring_method_ids:start
class ScoringMethodIds(StrEnum):
    TEMPLATE = "template"
    TASK_SCORE_UNWEIGHTED_AND_UNIVERSAL_PENALTY = (
        "task_score_unweighted_and_universal_penalty"
    )
    TASK_SCORE_AND_UNIVERSAL_PENALTY_WEIGHTED_SUM = (
        "task_score_and_universal_penalty_weighted_sum"
    )
    DEEP_RESEARCH_WEIGHTED_AVERAGE = "deep_research_weighted_average"
    # Apex V1 Grade Score - simple pass/fail ratio scoring
    APEX_V1_GRADE_SCORE = "apex_v1_grade_score"
    CRITERIA_LEVEL_CLAMPED_AVERAGE = "criteria_level_clamped_average"
    # ACE Criterion Score - with hurdle logic for ACE grading
    ACE_CRITERION_SCORE = "ace_criterion_score"


# @apg_scoring_method_ids:end


class ScoringConfig(BaseModel):
    """
    Scoring config model for scoring-based evaluation system.
    """

    scoring_config_id: str
    scoring_config_name: str
    scoring_defn_id: str
    scoring_config_values: dict[str, Any]
