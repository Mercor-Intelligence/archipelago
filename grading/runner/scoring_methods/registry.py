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

from .ace_criterion_score import ace_criterion_score_scoring
from .apex_v1_grade_score import apex_v1_grade_score_scoring
from .criteria_level_clamped_score import criteria_level_clamped_average_scoring
from .deep_research_weighted_average import deep_research_weighted_average_scoring
from .models import ScoringMethodCategory, ScoringMethodIds
from .task_score_unweighted import task_score_unweighted_scoring
from .task_score_weighted_sum import task_score_weighted_sum_scoring
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


# @apg_scoring_method_registry:start
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
    ScoringMethodIds.TASK_SCORE_AND_UNIVERSAL_PENALTY_WEIGHTED_SUM: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.TASK_SCORE_AND_UNIVERSAL_PENALTY_WEIGHTED_SUM,
        scoring_method_name="Task Score + Universal Penalty Weighted Sum Method",
        scoring_method_description="Applies different weight multipliers to primary and secondary objective verifiers, then subtracts a capped universal penalty.",
        category=ScoringMethodCategory.STANDARD,
        scoring_method_impl=task_score_weighted_sum_scoring,
        scoring_config_fields=[
            TaskFieldSchema(
                field_id="task_primary_objective_scaling_factor",
                field_type=TaskFieldType.NUMBER,
                label="Primary Objective Scaling Factor",
                description="Weight for primary objective verifiers",
                default_value=4.0,
                required=False,
            ),
            TaskFieldSchema(
                field_id="task_non_primary_objective_scaling_factor",
                field_type=TaskFieldType.NUMBER,
                label="Non-Primary Objective Scaling Factor",
                description="Weight for non-primary objective verifiers",
                default_value=2.0,
                required=False,
            ),
            TaskFieldSchema(
                field_id="task_negative_scaling_factor",
                field_type=TaskFieldType.NUMBER,
                label="Negative Scaling Factor",
                description="Weight for negative objectives (penalties)",
                default_value=0.2,
                required=False,
            ),
            TaskFieldSchema(
                field_id="universal_penalty_cap",
                field_type=TaskFieldType.NUMBER,
                label="Universal Penalty Cap",
                description="Maximum universal penalty as fraction",
                default_value=0.2,
                required=False,
            ),
            TaskFieldSchema(
                field_id="universal_total_negative_points",
                field_type=TaskFieldType.NUMBER,
                label="Total Negative Points",
                description="Total available negative points",
                default_value=100,
                required=False,
            ),
        ],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="task_score",
                field_type=TaskFieldType.NUMBER,
                label="Task Score",
                description="Normalized weighted task score (0-1)",
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
                field_id="cumulative_score",
                field_type=TaskFieldType.NUMBER,
                label="Cumulative Score",
                description="Weighted sum before normalization",
            ),
            TaskFieldSchema(
                field_id="max_possible_score",
                field_type=TaskFieldType.NUMBER,
                label="Max Possible Score",
                description="Maximum achievable weighted score",
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
    ScoringMethodIds.DEEP_RESEARCH_WEIGHTED_AVERAGE: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.DEEP_RESEARCH_WEIGHTED_AVERAGE,
        scoring_method_name="Deep Research Weighted Average",
        scoring_method_description="Calculates a weighted average using each verifier's numerical_weight field (defaults to 1.0).",
        category=ScoringMethodCategory.CUSTOM,
        scoring_method_impl=deep_research_weighted_average_scoring,
        scoring_config_fields=[],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="weighted_sum",
                field_type=TaskFieldType.NUMBER,
                label="Weighted Sum",
                description="Sum of (score × numerical_weight) for all verifiers",
            ),
            TaskFieldSchema(
                field_id="total_weights",
                field_type=TaskFieldType.NUMBER,
                label="Total Weights",
                description="Sum of all numerical_weight values",
            ),
            TaskFieldSchema(
                field_id="verifier_count",
                field_type=TaskFieldType.NUMBER,
                label="Verifier Count",
                description="Number of verifiers evaluated",
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
    ScoringMethodIds.CRITERIA_LEVEL_CLAMPED_AVERAGE: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.CRITERIA_LEVEL_CLAMPED_AVERAGE,
        scoring_method_name="Criteria-Level Clamped Average",
        scoring_method_description="Clamps negative verifier scores to 0, then calculates the average of all scores.",
        category=ScoringMethodCategory.STANDARD,
        scoring_method_impl=criteria_level_clamped_average_scoring,
        scoring_config_fields=[],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="total_count",
                field_type=TaskFieldType.NUMBER,
                label="Total Count",
                description="Total number of verifiers evaluated",
            ),
            TaskFieldSchema(
                field_id="positive_count",
                field_type=TaskFieldType.NUMBER,
                label="Positive Count",
                description="Number of verifiers with positive scores (> 0)",
            ),
            TaskFieldSchema(
                field_id="zero_count",
                field_type=TaskFieldType.NUMBER,
                label="Zero Count",
                description="Number of verifiers with zero scores",
            ),
            TaskFieldSchema(
                field_id="negative_count",
                field_type=TaskFieldType.NUMBER,
                label="Negative Count",
                description="Number of verifiers with negative scores (< 0, clamped to 0)",
            ),
            TaskFieldSchema(
                field_id="original_average",
                field_type=TaskFieldType.NUMBER,
                label="Original Average",
                description="Average score before clamping (for reference)",
            ),
            TaskFieldSchema(
                field_id="final_score_percentage",
                field_type=TaskFieldType.NUMBER,
                label="Final Score %",
                description="Final score as percentage (0-100)",
            ),
        ],
    ),
    ScoringMethodIds.ACE_CRITERION_SCORE: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.ACE_CRITERION_SCORE,
        scoring_method_name="ACE Criterion Score with Hurdle Logic",
        scoring_method_description="Converts each verifier score to +1/0/-1, then calculates sum divided by count. Hurdle failures set the score to 0.",
        category=ScoringMethodCategory.CUSTOM,
        scoring_method_impl=ace_criterion_score_scoring,
        scoring_config_fields=[],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="total_score",
                field_type=TaskFieldType.NUMBER,
                label="Total Score",
                description="Sum of all ACE criterion scores (before hurdle adjustment)",
            ),
            TaskFieldSchema(
                field_id="total_hurdle_score",
                field_type=TaskFieldType.NUMBER,
                label="Hurdle-Adjusted Score",
                description="0 if any hurdle fails, else total_score",
            ),
            TaskFieldSchema(
                field_id="pass_count",
                field_type=TaskFieldType.NUMBER,
                label="Pass Count",
                description="Number of criteria that passed (score = +1)",
            ),
            TaskFieldSchema(
                field_id="fail_response_count",
                field_type=TaskFieldType.NUMBER,
                label="Fail Response Count",
                description="Number of criteria that failed response text check (score = 0)",
            ),
            TaskFieldSchema(
                field_id="fail_source_count",
                field_type=TaskFieldType.NUMBER,
                label="Fail Source Count",
                description="Number of criteria that failed source verification (score = -1, hallucination)",
            ),
            TaskFieldSchema(
                field_id="hurdle_count",
                field_type=TaskFieldType.NUMBER,
                label="Hurdle Count",
                description="Number of hurdle criteria",
            ),
            TaskFieldSchema(
                field_id="hurdle_pass_count",
                field_type=TaskFieldType.NUMBER,
                label="Hurdle Pass Count",
                description="Number of hurdle criteria that passed",
            ),
            TaskFieldSchema(
                field_id="hurdle_all_pass",
                field_type=TaskFieldType.BOOLEAN,
                label="Hurdle All Pass",
                description="True if all hurdle criteria passed",
            ),
            TaskFieldSchema(
                field_id="total_count",
                field_type=TaskFieldType.NUMBER,
                label="Total Count",
                description="Total number of criteria evaluated",
            ),
        ],
    ),
}
# @apg_scoring_method_registry:end
