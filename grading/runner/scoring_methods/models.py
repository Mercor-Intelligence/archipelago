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
    # Apex V1 Grade Score - simple pass/fail ratio scoring
    APEX_V1_GRADE_SCORE = "apex_v1_grade_score"

class ScoringConfig(BaseModel):
    """
    Scoring config model for scoring-based evaluation system.
    """

    scoring_config_id: str
    scoring_config_name: str
    scoring_defn_id: str
    scoring_config_values: dict[str, Any]
