"""Auto-registration decorator for custom verifiers."""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from .types import VerifierConfig

T = TypeVar("T", bound=BaseModel)

_VERIFIER_REGISTRY: dict[str, dict[str, Any]] = {}

VerificationFunction = Callable[..., Awaitable[Any]]


def register_verifier(
    config: VerifierConfig,
    input_type: type[BaseModel],
) -> Callable[[VerificationFunction], VerificationFunction]:
    """
    Decorator to auto-register a verifier with validation.

    Validates naming conventions and that Pydantic fields match config input_fields.
    """

    def decorator(func: VerificationFunction) -> VerificationFunction:
        config_id = config.id
        expected_func_name = f"grade_{config_id}"

        if func.__name__ != expected_func_name:
            raise ValueError(
                f"Function name '{func.__name__}' doesn't match expected '{expected_func_name}'"
            )

        # Allow overwriting if upgrading from config-only to full registration
        if config_id in _VERIFIER_REGISTRY:
            existing = _VERIFIER_REGISTRY[config_id]
            if existing.get("function") is not None:
                # Already has a function, this is a duplicate
                raise ValueError(
                    f"Verifier '{config_id}' is already registered with a function"
                )

        _validate_input_type_matches_config(config, input_type)

        _VERIFIER_REGISTRY[config_id] = {
            "function": func,
            "config": config,
            "input_type": input_type,
        }
        return func

    return decorator


def _validate_input_type_matches_config(
    config: VerifierConfig, input_type: type[BaseModel]
) -> None:
    """Validates that Pydantic model fields match config input_fields."""
    pydantic_fields = set(input_type.model_fields.keys())
    config_fields = {field.input_id for field in config.input_fields}

    missing_in_pydantic = config_fields - pydantic_fields
    if missing_in_pydantic:
        raise ValueError(
            f"Config '{config.id}' defines {missing_in_pydantic} "
            f"missing from Pydantic model {input_type.__name__}"
        )

    extra_in_pydantic = pydantic_fields - config_fields
    if extra_in_pydantic:
        raise ValueError(
            f"Pydantic model {input_type.__name__} has extra fields {extra_in_pydantic} "
            f"not in config '{config.id}'"
        )

    for field_def in config.input_fields:
        field_id = field_def.input_id
        pydantic_field = input_type.model_fields[field_id]
        field_annotation = pydantic_field.annotation

        # Unwrap Optional types
        if hasattr(field_annotation, "__origin__"):
            import types
            from typing import get_args, get_origin

            origin = get_origin(field_annotation)
            if origin is types.UnionType or (
                origin is not None
                and hasattr(origin, "__name__")
                and origin.__name__ == "UnionType"
            ):
                args = get_args(field_annotation)
                if type(None) in args:
                    field_annotation = next(
                        arg for arg in args if arg is not type(None)
                    )

        _check_type_compatibility(config.id, field_id, field_def.type, field_annotation)


def _check_type_compatibility(
    config_id: str, field_id: str, config_type: Any, pydantic_annotation: Any
) -> None:
    """Check if config InputFieldType matches Pydantic annotation."""
    import types
    from typing import get_args, get_origin

    from .types import ArtifactSelection, InputFieldType

    origin = get_origin(pydantic_annotation)

    # Unwrap Optional one more time if needed
    if origin is types.UnionType or (
        origin is not None
        and hasattr(origin, "__name__")
        and origin.__name__ == "UnionType"
    ):
        args = get_args(pydantic_annotation)
        if type(None) in args:
            non_none_args = [arg for arg in args if arg is not type(None)]
            if len(non_none_args) == 1:
                pydantic_annotation = non_none_args[0]
                origin = get_origin(pydantic_annotation)

    def check_artifact_selection(t: Any) -> bool:
        if get_origin(t) is not list:
            return False
        args = get_args(t)
        if not args:
            return False
        return args[0] is ArtifactSelection

    type_checks = {
        InputFieldType.STR: lambda t: t is str,
        InputFieldType.INT: lambda t: t is int,
        InputFieldType.FLOAT: lambda t: t in (float, int),
        InputFieldType.BOOL: lambda t: t is bool,
        InputFieldType.JSON: lambda t: t in (dict, list, Any)
        or (get_origin(t) in (dict, list)),
        InputFieldType.MULTI_SELECT: lambda t: get_origin(t) is list,
        InputFieldType.SINGLE_SELECT: lambda t: t is str,
        InputFieldType.ARTIFACT_SELECTION: check_artifact_selection,
    }

    check = type_checks.get(config_type)
    if check and not check(pydantic_annotation):
        expected = (
            "list[ArtifactSelection]"
            if config_type == InputFieldType.ARTIFACT_SELECTION
            else config_type.value
        )
        raise ValueError(
            f"Type mismatch '{field_id}' in '{config_id}': "
            f"expected {expected}, got {pydantic_annotation}"
        )


def register_config(
    config: VerifierConfig,
    input_type: type[BaseModel],
) -> None:
    """
    Register a config and its input type without a verification function.

    This is used for lightweight registration where only config definitions
    are needed (e.g., in rl-studio/server) without archipelago dependencies.
    """
    config_id = config.id

    if config_id in _VERIFIER_REGISTRY:
        # If already registered with a function, skip (verification.py takes precedence)
        if _VERIFIER_REGISTRY[config_id].get("function") is not None:
            return

    _validate_input_type_matches_config(config, input_type)

    _VERIFIER_REGISTRY[config_id] = {
        "function": None,  # No function for config-only registration
        "config": config,
        "input_type": input_type,
    }


def get_all_verifiers() -> dict[str, dict[str, Any]]:
    """Get all registered verifiers."""
    return _VERIFIER_REGISTRY.copy()
