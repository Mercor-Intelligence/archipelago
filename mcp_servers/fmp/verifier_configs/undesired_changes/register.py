from ..decorators import register_config
from .config import UNDESIRED_CHANGES_CONFIG, UndesiredChangesInput

register_config(config=UNDESIRED_CHANGES_CONFIG, input_type=UndesiredChangesInput)
