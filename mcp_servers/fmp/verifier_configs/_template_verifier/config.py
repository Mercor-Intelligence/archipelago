from pydantic import BaseModel, Field

from ..helpers import config_from_pydantic
from ..types import InputFieldType


class YourVerifierInput(BaseModel):
    """
    Input model for your_verifier_name.

    Field mapping (full 1:1 from json_schema_extra):
      - description: Developer documentation (NOT used for UI)
      - json_schema_extra["input_label"]: UI label shown to users
      - json_schema_extra["input_subtitle"]: UI subtitle/help text shown to users
      - json_schema_extra["field_type"]: Explicit type override (optional)
      - json_schema_extra["options"]: Options for select types (optional)
    """

    example_criterion: str = Field(
        ...,
        description="Developer note",
        json_schema_extra={
            "input_label": "Evaluation Criterion",
            "input_subtitle": "What to evaluate",
            "field_type": InputFieldType.STR,
        },
    )


YOUR_VERIFIER_CONFIG = config_from_pydantic(
    config_id="your_verifier_name",
    name="Your Verifier Display Name",
    description="What this verifier evaluates and how it works.",
    input_model=YourVerifierInput,
)
