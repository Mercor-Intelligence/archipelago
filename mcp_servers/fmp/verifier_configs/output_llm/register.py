from ..decorators import register_config
from .config import OUTPUT_LLM_CONFIG, OutputLLMInput

register_config(config=OUTPUT_LLM_CONFIG, input_type=OutputLLMInput)
