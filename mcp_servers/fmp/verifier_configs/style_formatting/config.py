from pydantic import BaseModel

from ..helpers import config_from_pydantic


class StyleFormattingInput(BaseModel):
    pass


STYLE_FORMATTING_CONFIG = config_from_pydantic(
    config_id="style_formatting",
    name="Style & Formatting Verifier",
    description="Universal verifier that checks for serious stylistic or formatting failures "
    "in the agent's changes. Evaluates both text content and visual screenshots.",
    input_model=StyleFormattingInput,
)
