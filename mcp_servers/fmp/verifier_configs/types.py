from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class InputFieldType(str, Enum):
    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ARTIFACT_SELECTION = "artifact_selection"
    JSON = "json"
    MULTI_SELECT = "multi_select"
    SINGLE_SELECT = "single_select"


class ArtifactSelection(BaseModel):
    name: str
    source: Literal["world", "task"]
    snapshotId: str
    index: int | None = None


class InputFieldDefinition(BaseModel):
    input_id: str = Field(..., description="Unique identifier for this input field")
    input_label: str = Field(..., description="Human-readable label for the field")
    input_subtitle: str | None = Field(None, description="Optional subtitle/help text")
    type: InputFieldType = Field(..., description="Type of the input field")
    required: bool = Field(
        True, description="Whether this field is required (default: True)"
    )
    default_value: Any | None = Field(
        None, description="Default value for the field if not provided"
    )
    options: list[str] | None = Field(
        None,
        description="Available options for MULTI_SELECT or SINGLE_SELECT types",
    )
    input_hidden: bool = Field(
        False, description="Whether or not this field should be hidden"
    )


class VerifierConfig(BaseModel):
    id: str = Field(
        ..., description="Unique identifier for this config (e.g., 'output_llm')"
    )
    name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="Description of what this verifier does")
    input_fields: list[InputFieldDefinition] = Field(
        default_factory=list,
        description="List of input fields for this config (empty if no custom inputs)",
    )
