from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from runner.evals.models import EvalIds, EvalImplInput, EvalType
from runner.helpers.models import HelperIds
from runner.models import TaskFieldSchema, TaskFieldType, VerifierResult

from .output_llm import llm_judge_eval

EvalImpl = Callable[
    [EvalImplInput],
    Awaitable[VerifierResult],
]

class EvalDefn(BaseModel):
    eval_id: EvalIds
    eval_impl: EvalImpl | None = None  # Optional - server doesn't need implementation
    helper_dependencies: list[HelperIds]
    eval_types: list[EvalType] = []
    eval_config_fields: list[TaskFieldSchema]
    verifier_config_fields: list[TaskFieldSchema]
    verifier_output_fields: list[TaskFieldSchema]
    verifier_doc: str | None = None
    max_concurrency: int | None = None  # None = no limit (uses global limit only)

EVAL_REGISTRY: dict[EvalIds, EvalDefn] = {
    EvalIds.OUTPUT_LLM: EvalDefn(
        eval_id=EvalIds.OUTPUT_LLM,
        eval_impl=llm_judge_eval,
        helper_dependencies=[
            HelperIds.SNAPSHOT_DIFF,
            HelperIds.FINAL_ANSWER,
        ],
        eval_types=[EvalType.LLM_JUDGE],
        eval_config_fields=[],
        verifier_config_fields=[
            TaskFieldSchema(
                field_id="criteria",
                field_type=TaskFieldType.TEXTAREA,
                label="Criteria",
                description="What should be verified in the output?",
                required=True,
            ),
            TaskFieldSchema(
                field_id="criteria_explanation",
                field_type=TaskFieldType.TEXTAREA,
                label="Criteria Explanation",
                description="Additional context for the criteria",
                required=False,
            ),
            TaskFieldSchema(
                field_id="weight",
                field_type=TaskFieldType.NUMBER,
                label="Weight",
                description="Weight for scoring (default: 1.0)",
                default_value=1.0,
                required=False,
                display_hidden=True,
            ),
            TaskFieldSchema(
                field_id="tags",
                field_type=TaskFieldType.MULTISELECT,
                label="Tags",
                description="What is being evaluated",
                default_value=["Statement"],
                options=[
                    "Statement",
                    "Reasoning (numerical)",
                    "Reasoning (qualitative)",
                    "Style / formatting",
                    "Editing an existing file",
                    "Editing an existing file - make no unrequested changes",
                    "Final Response",
                    "Tool Use",
                    "Extraction",
                ],
                required=False,
            ),
            TaskFieldSchema(
                field_id="is_primary_objective",
                field_type=TaskFieldType.BOOLEAN,
                label="Is this a primary criterion?",
                description="Designates the importance of the criterion for reporting purposes.",
                default_value=True,
                required=True,
            ),
            TaskFieldSchema(
                field_id="autogen_source",
                field_type=TaskFieldType.TEXT,
                label="Autogen Source",
                description="Source of auto-generated verifiers",
                required=False,
                display_hidden=True,
            ),
            TaskFieldSchema(
                field_id="expected_file_type",
                field_type=TaskFieldType.SELECT,
                label="Grading Target",
                description="Which sub-part of the output should be evaluated for this criterion.",
                options=[
                    "All output (modified files and final message in console)",
                    "Final Answer Only (No Files)",
                    "Word Documents (.docx, .doc)",
                    "Text Files (.txt)",
                    "PDF Documents (.pdf)",
                    "Spreadsheets (.xlsx, .xls, .xlsm)",
                    "Presentations (.pptx, .ppt)",
                    "Python Files (.py)",
                    "JavaScript/TypeScript (.js, .ts, .jsx, .tsx)",
                    "Markdown (.md)",
                    "JSON/YAML (.json, .yaml, .yml)",
                    "Images (.png, .jpg, .jpeg, .webp)",
                ],
                default_value="All output (modified files and final message in console)",
                required=True,
            ),
            TaskFieldSchema(
                field_id="artifacts_to_reference",
                field_type=TaskFieldType.ARTIFACT_MULTISELECT,
                label="Reference Artifacts",
                description="Select files to provide as context for grading",
                required=False,
            ),
        ],
        verifier_output_fields=[
            TaskFieldSchema(
                field_id="judge_grade",
                field_type=TaskFieldType.TEXT,
                label="Judge Grade",
                description="Pass or fail grade from LLM",
            ),
            TaskFieldSchema(
                field_id="grade_rationale",
                field_type=TaskFieldType.TEXTAREA,
                label="Rationale",
                description="Explanation for the grade",
            ),
            TaskFieldSchema(
                field_id="evaluated_artifacts",
                field_type=TaskFieldType.TEXT,
                label="Evaluated Artifacts",
                description="Files that were evaluated for this criterion",
                required=False,
            ),
        ],
    ),
    EvalIds.OUTPUT_LLM_LITE: EvalDefn(
        eval_id=EvalIds.OUTPUT_LLM_LITE,
        eval_impl=llm_judge_eval,
        helper_dependencies=[
            HelperIds.SNAPSHOT_DIFF,
            HelperIds.FINAL_ANSWER,
        ],
        eval_types=[EvalType.LLM_JUDGE],
        eval_config_fields=[],
        verifier_config_fields=[
            TaskFieldSchema(
                field_id="criteria",
                field_type=TaskFieldType.TEXTAREA,
                label="Criteria",
                description="What should be verified in the output?",
                required=True,
            ),
            TaskFieldSchema(
                field_id="criteria_explanation",
                field_type=TaskFieldType.TEXTAREA,
                label="Criteria Explanation",
                description="Additional context for the criteria",
                required=False,
            ),
            TaskFieldSchema(
                field_id="is_primary_objective",
                field_type=TaskFieldType.BOOLEAN,
                label="Is this a primary criterion?",
                description="Designates the importance of the criterion for reporting purposes.",
                default_value=True,
                required=True,
            ),
            TaskFieldSchema(
                field_id="expected_file_type",
                field_type=TaskFieldType.SELECT,
                label="Grading Target",
                description="Which sub-part of the output should be evaluated for this criterion: the agent's final console message or the files it modified? ONLY the selected target will be graded.",
                options=[
                    "All output (modified files and final message in console)",
                    "Final Answer Only (No Files)",
                    "Word Documents (.docx, .doc)",
                    "Text Files (.txt)",
                    "PDF Documents (.pdf)",
                    "Spreadsheets (.xlsx, .xls, .xlsm)",
                    "Presentations (.pptx, .ppt)",
                    "Python Files (.py)",
                    "JavaScript/TypeScript (.js, .ts, .jsx, .tsx)",
                    "Markdown (.md)",
                    "JSON/YAML (.json, .yaml, .yml)",
                    "Images (.png, .jpg, .jpeg, .webp)",
                ],
                default_value="All output (modified files and final message in console)",
                required=True,
            ),
        ],
        verifier_output_fields=[
            TaskFieldSchema(
                field_id="judge_grade",
                field_type=TaskFieldType.TEXT,
                label="Judge Grade",
                description="Pass or fail grade from LLM",
            ),
            TaskFieldSchema(
                field_id="grade_rationale",
                field_type=TaskFieldType.TEXTAREA,
                label="Rationale",
                description="Explanation for the grade",
            ),
            TaskFieldSchema(
                field_id="evaluated_artifacts",
                field_type=TaskFieldType.TEXT,
                label="Evaluated Artifacts",
                description="Files that were evaluated for this criterion",
                required=False,
            ),
        ],
    ),
}
