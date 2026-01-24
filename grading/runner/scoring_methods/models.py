"""
Models for scoring method definitions.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ScoringMethodIds(StrEnum):
    TEMPLATE = "template"
    APEX_V1_GRADE_SCORE = "apex_v1_grade_score"


class ScoringConfig(BaseModel):
    scoring_config_id: str
    scoring_config_name: str
    scoring_defn_id: str
    scoring_config_values: dict[str, Any]
