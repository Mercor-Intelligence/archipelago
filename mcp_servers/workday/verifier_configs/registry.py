"""Auto-discovers and registers all custom verifiers."""

import importlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .decorators import get_all_verifiers
from .types import VerifierConfig

# Optional logging - not available in rl-studio/server
try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

VerificationFunction = Callable[..., Awaitable[Any]]


def _auto_discover_verifiers() -> None:
    """Scan configs directory and import all register.py and verification.py modules.

    Priority order:
    1. First import register.py (lightweight, no archipelago deps) if it exists
    2. Then import verification.py (full with function) if it exists, which will override
    """
    configs_dir = Path(__file__).parent

    for item in configs_dir.iterdir():
        if not item.is_dir() or item.name.startswith("_") or item.name == "__pycache__":
            continue

        # First try to import register.py (lightweight config-only registration)
        register_file = item / "register.py"
        if register_file.exists():
            try:
                module_path = f".{item.name}.register"
                importlib.import_module(module_path, package=__package__)
                logger.debug(f"Registered config from {item.name}/register.py")
            except Exception as e:
                logger.error(
                    f"[VERIFIER_GRADING_ERROR] REGISTRY_CONFIG_IMPORT | "
                    f"config_name={item.name} | "
                    f"error_type={type(e).__name__} | "
                    f"error_message={str(e)}"
                )
                raise

        # Then try verification.py (full registration with function)
        # This will override the config-only registration if both exist
        verification_file = item / "verification.py"
        if verification_file.exists():
            try:
                module_path = f".{item.name}.verification"
                importlib.import_module(module_path, package=__package__)
                logger.debug(f"Registered verifier from {item.name}/verification.py")
            except Exception as e:
                logger.error(
                    f"[VERIFIER_GRADING_ERROR] REGISTRY_VERIFIER_IMPORT | "
                    f"config_name={item.name} | "
                    f"error_type={type(e).__name__} | "
                    f"error_message={str(e)}"
                )
                raise


_auto_discover_verifiers()
_verifiers = get_all_verifiers()

VERIFIER_CONFIG_REGISTRY: dict[str, VerificationFunction] = {
    config_id: data["function"]
    for config_id, data in _verifiers.items()
    if data["function"] is not None
}

CONFIG_DEFINITIONS: dict[str, VerifierConfig] = {
    config_id: data["config"] for config_id, data in _verifiers.items()
}

CONFIG_INPUT_TYPES: dict[str, type[BaseModel]] = {
    config_id: data["input_type"] for config_id, data in _verifiers.items()
}


def get_verification_function(config_id: str) -> VerificationFunction:
    if config_id not in VERIFIER_CONFIG_REGISTRY:
        raise ValueError(
            f"Unknown config_id '{config_id}'. "
            f"Available configs: {', '.join(VERIFIER_CONFIG_REGISTRY.keys())}"
        )
    return VERIFIER_CONFIG_REGISTRY[config_id]


def get_config_definition(config_id: str) -> VerifierConfig:
    if config_id not in CONFIG_DEFINITIONS:
        raise ValueError(f"Unknown config_id '{config_id}'")
    return CONFIG_DEFINITIONS[config_id]


def get_config_input_type(config_id: str) -> type[BaseModel]:
    if config_id not in CONFIG_INPUT_TYPES:
        raise ValueError(f"Unknown config_id '{config_id}'")
    return CONFIG_INPUT_TYPES[config_id]
