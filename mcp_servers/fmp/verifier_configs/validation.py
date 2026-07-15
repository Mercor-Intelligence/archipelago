from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .types import ArtifactSelection, InputFieldType, VerifierConfig

T = TypeVar("T", bound=BaseModel)


def validate_config_input(  # noqa: UP047
    config_input: dict[str, Any] | None,
    config: VerifierConfig,
    input_model: type[T],
) -> T:
    if config_input is None:
        config_input = {}

    # Check all required fields are present
    for field_def in config.input_fields:
        if field_def.required and field_def.input_id not in config_input:
            if field_def.default_value is not None:
                config_input[field_def.input_id] = field_def.default_value
            else:
                raise ValueError(
                    f"Required field '{field_def.input_id}' missing from config_input for config '{config.id}'"
                )

    # Basic type validation for each field
    for field_id, value in config_input.items():
        field_def = next(
            (f for f in config.input_fields if f.input_id == field_id), None
        )
        if field_def is None:
            raise ValueError(
                f"Unknown field '{field_id}' in config_input for config '{config.id}'"
            )

        if value is not None:
            if field_def.type == InputFieldType.STR and not isinstance(value, str):
                raise ValueError(
                    f"Field '{field_id}' must be a string, got {type(value).__name__}"
                )
            elif field_def.type == InputFieldType.INT and not isinstance(value, int):
                raise ValueError(
                    f"Field '{field_id}' must be an integer, got {type(value).__name__}"
                )
            elif field_def.type == InputFieldType.FLOAT and not isinstance(
                value, (int, float)
            ):
                raise ValueError(
                    f"Field '{field_id}' must be a number, got {type(value).__name__}"
                )
            elif field_def.type == InputFieldType.BOOL and not isinstance(value, bool):
                raise ValueError(
                    f"Field '{field_id}' must be a boolean, got {type(value).__name__}"
                )
            elif field_def.type == InputFieldType.MULTI_SELECT:
                if not isinstance(value, list):
                    raise ValueError(
                        f"Field '{field_id}' must be a list, got {type(value).__name__}"
                    )
                if field_def.options and not all(v in field_def.options for v in value):
                    raise ValueError(
                        f"Field '{field_id}' contains invalid options. Valid options: {field_def.options}"
                    )
            elif field_def.type == InputFieldType.SINGLE_SELECT:
                if not isinstance(value, str):
                    raise ValueError(
                        f"Field '{field_id}' must be a string, got {type(value).__name__}"
                    )
                if field_def.options and value not in field_def.options:
                    raise ValueError(
                        f"Field '{field_id}' must be one of {field_def.options}, got '{value}'"
                    )
            elif field_def.type == InputFieldType.ARTIFACT_SELECTION:
                if not isinstance(value, list):
                    raise ValueError(
                        f"Field '{field_id}' must be a list, got {type(value).__name__}"
                    )
                for i, item in enumerate(value):
                    # Accept both dict (from JSON/API) and ArtifactSelection (already validated)
                    if not isinstance(item, (dict, ArtifactSelection)):
                        raise ValueError(
                            f"Field '{field_id}[{i}]' must be a dict or ArtifactSelection, got {type(item).__name__}"
                        )
                    # Only validate dicts; ArtifactSelection instances are already validated
                    if isinstance(item, dict):
                        try:
                            ArtifactSelection.model_validate(item)
                        except ValidationError as e:
                            raise ValueError(
                                f"Field '{field_id}[{i}]' validation failed: {str(e)}"
                            ) from e

    # Parse into Pydantic model for full validation
    try:
        return input_model.model_validate(config_input)
    except ValidationError as e:
        raise ValueError(
            f"Config input validation failed for config '{config.id}': {str(e)}"
        ) from e


def get_typed_config_input(verifier: Any) -> BaseModel:
    """
    Parse config_input from a verifier object into typed Pydantic model based on config_id.

    Expects verifier to have config_id and config_input attributes.
    Returns Pydantic model defined in config.
    """
    from .registry import get_config_definition, get_config_input_type

    config_def = get_config_definition(verifier.config_id)
    input_type = get_config_input_type(verifier.config_id)
    return validate_config_input(verifier.config_input, config_def, input_type)
