from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from runner.models import (
    ScoringMethodResult,
    TaskFieldSchema,
    TaskFieldType,
    Verifier,
    VerifierResult,
)

from .apex_v1_grade_score import apex_v1_grade_score_scoring
from .models import ScoringMethodCategory, ScoringMethodIds
from .task_score_unweighted import task_score_unweighted_scoring
from .template import template_scoring_method

class ScoringMethodDefn(BaseModel):
    scoring_method_id: ScoringMethodIds
    scoring_method_name: str
    scoring_method_description: str | None = None
    category: ScoringMethodCategory
    scoring_method_impl: (
        Callable[
            [list[VerifierResult], list[Verifier], dict[str, Any]],
            Awaitable[ScoringMethodResult],
        ]
        | None
    ) = None  # Optional - server doesn't need implementation
    scoring_config_fields: list[TaskFieldSchema]
    scoring_output_fields: list[TaskFieldSchema] | None = None

SCORING_METHOD_REGISTRY: dict[ScoringMethodIds, ScoringMethodDefn] = {
    ScoringMethodIds.TEMPLATE: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.TEMPLATE,
        scoring_method_name="Template Scoring Method",
        scoring_method_description="Base template for creating custom scoring methods.",
        category=ScoringMethodCategory.STANDARD,
        scoring_method_impl=template_scoring_method,
        scoring_config_fields=[],
        scoring_output_fields=[],
    ),
    ScoringMethodIds.TASK_SCORE_UNWEIGHTED_AND_UNIVERSAL_PENALTY: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.TASK_SCORE_UNWEIGHTED_AND_UNIVERSAL_PENALTY,
        scoring_method_name="Task Score Unweighted + Universal Penalty Method",
        scoring_method_description="Calculates a simple average of task verifier scores, then subtracts a capped penalty from universal verifiers.",
        category=ScoringMethodCategory.STANDARD,
        scoring_method_impl=task_score_unweighted_scoring,
        scoring_config_fields=[
            TaskFieldSchema(
                field_id="universal_penalty_cap",
                field_type=TaskFieldType.NUMBER,
                label="Universal Penalty Cap",
                description="Maximum universal penalty as fraction (0.2 = 20%)",
                default_value=0.2,
                required=False,
            ),
            TaskFieldSchema(
                field_id="universal_total_negative_points",
                field_type=TaskFieldType.NUMBER,
                label="Total Negative Points",
                description="Total available negative points for percentage calculation",
                default_value=100,
                required=False,
            ),
        ],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="task_score",
                field_type=TaskFieldType.NUMBER,
                label="Task Score",
                description="Normalized task verifier score (0-1)",
            ),
            TaskFieldSchema(
                field_id="universal_penalty",
                field_type=TaskFieldType.NUMBER,
                label="Universal Penalty",
                description="Universal penalty as fraction",
            ),
            TaskFieldSchema(
                field_id="capped_penalty",
                field_type=TaskFieldType.NUMBER,
                label="Capped Penalty",
                description="Universal penalty after applying cap",
            ),
            TaskFieldSchema(
                field_id="task_verifier_count",
                field_type=TaskFieldType.NUMBER,
                label="Task Verifier Count",
                description="Number of task-specific verifiers",
            ),
            TaskFieldSchema(
                field_id="universal_verifier_count",
                field_type=TaskFieldType.NUMBER,
                label="Universal Verifier Count",
                description="Number of universal verifiers",
            ),
        ],
    ),
    ScoringMethodIds.APEX_V1_GRADE_SCORE: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.APEX_V1_GRADE_SCORE,
        scoring_method_name="Apex V1 Grade Score",
        scoring_method_description="Counts passed criteria (score >= 0.99) and returns the pass rate as the final score.",
        category=ScoringMethodCategory.CUSTOM,
        scoring_method_impl=apex_v1_grade_score_scoring,
        scoring_config_fields=[],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="passed_count",
                field_type=TaskFieldType.NUMBER,
                label="Passed Count",
                description="Number of criteria that passed (score = 1)",
            ),
            TaskFieldSchema(
                field_id="failed_count",
                field_type=TaskFieldType.NUMBER,
                label="Failed Count",
                description="Number of criteria that failed (score = 0)",
            ),
            TaskFieldSchema(
                field_id="total_count",
                field_type=TaskFieldType.NUMBER,
                label="Total Count",
                description="Total number of criteria evaluated",
            ),
            TaskFieldSchema(
                field_id="grade_score_percentage",
                field_type=TaskFieldType.NUMBER,
                label="Grade Score %",
                description="Grade score as percentage (0-100)",
            ),
        ],
    ),
}
