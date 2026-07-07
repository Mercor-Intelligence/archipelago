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
from .deep_research_weighted_average_with_gates import (
    deep_research_weighted_average_with_gates_scoring,
)
from .difficulty_weighted_average import difficulty_weighted_average_scoring
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
        scoring_method_description=(
            "Calculates a weighted average. Per-verifier weight is read from "
            "verifier_values['numerical_weight'] by default. To weight by a "
            "world-level custom verifier metadata field instead (e.g. a "
            "'Weight' Select field on a custom rubric), set "
            "weight_custom_field_id to that field_id; the impl will read "
            "verifier.verifier_custom_field_values[<id>] (coerced to float) "
            "and fall back to numerical_weight, then 1.0."
        ),
        category=ScoringMethodCategory.CUSTOM,
        scoring_method_impl=deep_research_weighted_average_scoring,
        scoring_config_fields=[
            TaskFieldSchema(
                field_id="weight_custom_field_id",
                field_type=TaskFieldType.TEXT,
                label="Weight Custom Field ID",
                description=(
                    "Optional. field_id of a verifier_custom_fields_schema "
                    "entry (e.g. 'field_d82a851d01714519b104ff8f7f59df19') "
                    "whose per-verifier value should be used as the weight. "
                    "When unset, falls back to verifier_values['numerical_weight']."
                ),
                required=False,
            ),
        ],
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
    ScoringMethodIds.DEEP_RESEARCH_WEIGHTED_AVERAGE_WITH_GATES: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.DEEP_RESEARCH_WEIGHTED_AVERAGE_WITH_GATES,
        scoring_method_name="Deep Research Weighted Average + Gates",
        scoring_method_description=(
            "Same as Deep Research Weighted Average, plus configurable score caps "
            "for an Expert Assessment pass-rate floor and Gate-tagged verifier "
            "failures. Lowest cap wins on multi-trigger. After the cap layer "
            "settles on a score, a multiplicative Critical Value haircut is "
            "applied: for each failed verifier tagged with a "
            "``critical_value_field_values`` entry (default ``Gate: Critical "
            "Value``), the score is multiplied by ``critical_value_multiplier`` "
            "(default 0.8). Final = capped_score × multiplier^N. Workbench / "
            "Goodhart's-law guard: tasks rarely need more than a handful of "
            "Critical Value criteria; the ``critical_value_max_count`` field is "
            "an advisory limit surfaced in the editor (default 5)."
        ),
        category=ScoringMethodCategory.CUSTOM,
        scoring_method_impl=deep_research_weighted_average_with_gates_scoring,
        scoring_config_fields=[
            TaskFieldSchema(
                field_id="expert_assessment_values",
                field_type=TaskFieldType.JSON,
                label="Expert Assessment Values",
                description=(
                    "Custom-field values that mark a verifier as an Expert "
                    "Assessment criterion. Defaults to "
                    '["Expert Assessment"].'
                ),
                default_value=["Expert Assessment"],
                required=False,
            ),
            TaskFieldSchema(
                field_id="expert_assessment_pass_rate_threshold",
                field_type=TaskFieldType.NUMBER,
                label="Expert Assessment Pass-Rate Threshold",
                description=(
                    "If the share of passing Expert Assessment verifiers falls "
                    "below this fraction, the floor cap is triggered."
                ),
                default_value=0.25,
                min_value=0,
                max_value=1,
                required=False,
            ),
            TaskFieldSchema(
                field_id="expert_assessment_floor_cap",
                field_type=TaskFieldType.NUMBER,
                label="Expert Assessment Floor Cap",
                description="Score cap applied when the Expert Assessment floor triggers.",
                default_value=0.30,
                min_value=0,
                max_value=1,
                required=False,
            ),
            TaskFieldSchema(
                field_id="pass_score_threshold",
                field_type=TaskFieldType.NUMBER,
                label="Pass Score Threshold",
                description=(
                    "Minimum verifier score to count as passing for Expert "
                    "Assessment, Gate, and Critical Value evaluation."
                ),
                default_value=0.5,
                min_value=0,
                max_value=1,
                required=False,
            ),
            TaskFieldSchema(
                field_id="gate_caps",
                field_type=TaskFieldType.JSON,
                label="Gate Caps",
                description=(
                    "Map of Gate custom-field value → score cap. A failed "
                    "verifier whose custom field equals one of these keys "
                    "applies the corresponding cap."
                ),
                default_value={
                    "Gate: Missing Scope": 0.50,
                    "Gate: Ethical / Safety Violation": 0.40,
                },
                required=False,
            ),
            TaskFieldSchema(
                field_id="critical_value_field_values",
                field_type=TaskFieldType.JSON,
                label="Critical Value Field Values",
                description=(
                    "Custom-field values that mark a verifier as a Critical "
                    "Value criterion. Each failed (score < pass_score_threshold) "
                    "Critical Value verifier multiplies the final score by "
                    "``critical_value_multiplier``. Defaults to "
                    '["Gate: Critical Value"]. Set to [] to disable the '
                    "Critical Value haircut entirely."
                ),
                default_value=["Gate: Critical Value"],
                required=False,
            ),
            TaskFieldSchema(
                field_id="critical_value_multiplier",
                field_type=TaskFieldType.NUMBER,
                label="Critical Value Multiplier",
                description=(
                    "Per-failure multiplier applied to the score after the cap "
                    "layer settles. N failures compound multiplicatively: "
                    "final = capped × multiplier^N. Default 0.8 per the "
                    "Workbench Goodhart's-law guard."
                ),
                default_value=0.8,
                min_value=0,
                max_value=1,
                required=False,
            ),
            TaskFieldSchema(
                field_id="critical_value_max_count",
                field_type=TaskFieldType.NUMBER,
                label="Critical Value Max Count",
                description=(
                    "Advisory cap on how many criteria per rubric should be "
                    "tagged as Critical Value. Surfaced as a warning in the "
                    "editor; not enforced at score time."
                ),
                default_value=5,
                min_value=0,
                required=False,
            ),
        ],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="base_score",
                field_type=TaskFieldType.NUMBER,
                label="Base Score",
                description="Weighted average before any caps were applied",
            ),
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
            TaskFieldSchema(
                field_id="expert_assessment_total",
                field_type=TaskFieldType.NUMBER,
                label="Expert Assessment Total",
                description="Number of verifiers tagged as Expert Assessment",
            ),
            TaskFieldSchema(
                field_id="expert_assessment_passed",
                field_type=TaskFieldType.NUMBER,
                label="Expert Assessment Passed",
                description="Number of Expert Assessment verifiers that passed",
            ),
            TaskFieldSchema(
                field_id="expert_assessment_pass_rate",
                field_type=TaskFieldType.NUMBER,
                label="Expert Assessment Pass Rate",
                description="passed / total for Expert Assessment verifiers",
            ),
            TaskFieldSchema(
                field_id="expert_assessment_floor_triggered",
                field_type=TaskFieldType.BOOLEAN,
                label="Expert Assessment Floor Triggered",
                description="True if the Expert Assessment floor cap was applied",
            ),
            TaskFieldSchema(
                field_id="triggered_gates",
                field_type=TaskFieldType.JSON,
                label="Triggered Gates",
                description="List of gate failures: {verifier_id, gate_value, cap, score}",
            ),
            TaskFieldSchema(
                field_id="triggered_caps",
                field_type=TaskFieldType.JSON,
                label="Triggered Caps",
                description="List of caps applied to the base score (lowest wins)",
            ),
            TaskFieldSchema(
                field_id="critical_value_total",
                field_type=TaskFieldType.NUMBER,
                label="Critical Value Total",
                description="Number of verifiers tagged as Critical Value",
            ),
            TaskFieldSchema(
                field_id="critical_value_failed",
                field_type=TaskFieldType.NUMBER,
                label="Critical Value Failed (N)",
                description=(
                    "Number of failed Critical Value verifiers — the exponent N "
                    "in the multiplier^N haircut."
                ),
            ),
            TaskFieldSchema(
                field_id="critical_value_multiplier_applied",
                field_type=TaskFieldType.NUMBER,
                label="Critical Value Multiplier Applied",
                description=(
                    "Effective multiplier applied to the capped score: "
                    "critical_value_multiplier^critical_value_failed. 1.0 = no "
                    "haircut."
                ),
            ),
            TaskFieldSchema(
                field_id="score_before_critical_value",
                field_type=TaskFieldType.NUMBER,
                label="Score Before Critical Value",
                description=(
                    "Score after the gate/expert-assessment cap layer but "
                    "before the Critical Value multiplicative haircut."
                ),
            ),
            TaskFieldSchema(
                field_id="failed_critical_values",
                field_type=TaskFieldType.JSON,
                label="Failed Critical Values",
                description=(
                    "List of failures: "
                    "{verifier_id, field_value, score}. Surfaced so reviewers "
                    "can see which Critical Value criteria triggered the "
                    "haircut."
                ),
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
    ScoringMethodIds.DIFFICULTY_WEIGHTED_AVERAGE: ScoringMethodDefn(
        scoring_method_id=ScoringMethodIds.DIFFICULTY_WEIGHTED_AVERAGE,
        scoring_method_name="Difficulty Weighted Average",
        scoring_method_description=(
            "Reads each verifier's `difficulty` field (Low/Medium/High) and "
            "computes a weighted average. Weights default to Low=1, Medium=3, "
            "High=5 and are configurable per scoring config."
        ),
        category=ScoringMethodCategory.STANDARD,
        scoring_method_impl=difficulty_weighted_average_scoring,
        scoring_config_fields=[
            TaskFieldSchema(
                field_id="low_weight",
                field_type=TaskFieldType.NUMBER,
                label="Low Difficulty Weight",
                description="Points awarded to a passing Low-difficulty criterion",
                default_value=1.0,
                min_value=0,
                required=False,
            ),
            TaskFieldSchema(
                field_id="medium_weight",
                field_type=TaskFieldType.NUMBER,
                label="Medium Difficulty Weight",
                description="Points awarded to a passing Medium-difficulty criterion",
                default_value=3.0,
                min_value=0,
                required=False,
            ),
            TaskFieldSchema(
                field_id="high_weight",
                field_type=TaskFieldType.NUMBER,
                label="High Difficulty Weight",
                description="Points awarded to a passing High-difficulty criterion",
                default_value=5.0,
                min_value=0,
                required=False,
            ),
            TaskFieldSchema(
                field_id="default_difficulty",
                field_type=TaskFieldType.SELECT,
                label="Default Difficulty",
                description="Fallback difficulty used when a verifier omits the `difficulty` field.",
                options=["Low", "Medium", "High"],
                default_value="Medium",
                required=False,
            ),
        ],
        scoring_output_fields=[
            TaskFieldSchema(
                field_id="weighted_sum",
                field_type=TaskFieldType.NUMBER,
                label="Weighted Sum",
                description="Sum of (score × difficulty weight) across all verifiers",
            ),
            TaskFieldSchema(
                field_id="total_weights",
                field_type=TaskFieldType.NUMBER,
                label="Total Weights",
                description="Sum of difficulty weights for all scored verifiers",
            ),
            TaskFieldSchema(
                field_id="verifier_count",
                field_type=TaskFieldType.NUMBER,
                label="Verifier Count",
                description="Number of verifiers contributing to the score",
            ),
            TaskFieldSchema(
                field_id="low_count",
                field_type=TaskFieldType.NUMBER,
                label="Low Difficulty Count",
                description="Number of Low-difficulty verifiers scored",
            ),
            TaskFieldSchema(
                field_id="medium_count",
                field_type=TaskFieldType.NUMBER,
                label="Medium Difficulty Count",
                description="Number of Medium-difficulty verifiers scored",
            ),
            TaskFieldSchema(
                field_id="high_count",
                field_type=TaskFieldType.NUMBER,
                label="High Difficulty Count",
                description="Number of High-difficulty verifiers scored",
            ),
            TaskFieldSchema(
                field_id="skipped_zero_weight",
                field_type=TaskFieldType.NUMBER,
                label="Skipped (zero weight)",
                description="Number of verifiers excluded because their difficulty mapped to weight 0",
            ),
            TaskFieldSchema(
                field_id="final_score_percentage",
                field_type=TaskFieldType.NUMBER,
                label="Final Score %",
                description="Final score as percentage (0-100)",
            ),
            TaskFieldSchema(
                field_id="low_weight",
                field_type=TaskFieldType.NUMBER,
                label="Resolved Low Weight",
                description="Weight applied to Low-difficulty verifiers for this run",
            ),
            TaskFieldSchema(
                field_id="medium_weight",
                field_type=TaskFieldType.NUMBER,
                label="Resolved Medium Weight",
                description="Weight applied to Medium-difficulty verifiers for this run",
            ),
            TaskFieldSchema(
                field_id="high_weight",
                field_type=TaskFieldType.NUMBER,
                label="Resolved High Weight",
                description="Weight applied to High-difficulty verifiers for this run",
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
