from pydantic import BaseModel, Field

from ..helpers import config_from_pydantic
from ..types import ArtifactSelection, InputFieldType


class OutputLLMInput(BaseModel):
    criteria: str = Field(
        ...,
        description="What should be verified in the output?",
        json_schema_extra={
            "input_label": "Criteria",
            "input_subtitle": "What should be verified in the output?",
            "field_type": InputFieldType.STR,
        },
    )

    negative_criteria: str | None = Field(
        None,
        description="Describe what should NOT happen...",
        json_schema_extra={
            "input_label": "Negative Criteria",
            "input_subtitle": "Describe what should NOT happen...",
            "field_type": InputFieldType.STR,
        },
    )

    criteria_explanation: str | None = Field(
        None,
        description="Additional context...",
        json_schema_extra={
            "input_label": "Criteria Explanation",
            "input_subtitle": "Additional context...",
            "field_type": InputFieldType.STR,
        },
    )

    artifacts_to_reference: list[ArtifactSelection] | None = Field(
        None,
        description="Optional context documents to reference",
        json_schema_extra={
            "input_label": "Artifacts to Reference",
            "input_subtitle": "Optional context documents to reference",
            "field_type": InputFieldType.ARTIFACT_SELECTION,
        },
    )

    weight: float = Field(
        1.0,
        description="Weight of this verifier for scoring",
        json_schema_extra={
            "input_label": "Weight",
            "input_subtitle": "Weight of this verifier for scoring (default: 1.0)",
            "field_type": InputFieldType.FLOAT,
            "input_hidden": True,
        },
    )

    tags: list[str] | None = Field(
        ["Statement"],
        description="What is being evaluated",
        json_schema_extra={
            "input_label": "Tag",
            "input_subtitle": "What is being evaluated",
            "field_type": InputFieldType.MULTI_SELECT,
            "options": [
                "Statement",
                "Reasoning (numerical)",
                "Reasoning (qualitative)",
                "Style / formatting",
                "Editing an existing file",
                "Editing an existing file - make no unrequested changes",
                "Final Response",
            ],
        },
    )

    is_primary_objective: bool | None = Field(
        None,
        description="Whether this is a primary objective verifier",
        json_schema_extra={
            "input_label": "Is Primary Objective",
            "input_subtitle": "Whether this is a primary objective verifier",
            "field_type": InputFieldType.BOOL,
        },
    )

    modality: str | None = Field(
        "Free text",
        description="Grading modality",
        json_schema_extra={
            "input_label": "Modality",
            "input_subtitle": "Grading modality",
            "field_type": InputFieldType.SINGLE_SELECT,
            "options": ["Free text", "Images", "All content"],
            "input_hidden": True,
        },
    )

    autogen_source: str | None = Field(
        None,
        description="Source of auto-generated verifiers",
        json_schema_extra={
            "input_label": "Autogen Source",
            "input_subtitle": "Source of auto-generated verifiers",
            "field_type": InputFieldType.STR,
            "input_hidden": True,
        },
    )


OUTPUT_LLM_CONFIG = config_from_pydantic(
    config_id="output_llm",
    name="Output LLM Verifier",
    description="Evaluates agent changes and final answer using LLM-based grading against specified criteria.",
    input_model=OutputLLMInput,
)
