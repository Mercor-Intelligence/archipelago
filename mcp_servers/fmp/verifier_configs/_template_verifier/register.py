"""Lightweight registration without archipelago dependencies."""

from ..decorators import register_config
from .config import YOUR_VERIFIER_CONFIG, YourVerifierInput

register_config(config=YOUR_VERIFIER_CONFIG, input_type=YourVerifierInput)
