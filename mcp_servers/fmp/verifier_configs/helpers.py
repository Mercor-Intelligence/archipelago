from typing import Any, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .types import InputFieldDefinition, InputFieldType, VerifierConfig


def _infer_field_type(
    annotation: Any, field_info: FieldInfo, json_schema_extra: dict[str, Any] | None
) -> InputFieldType:
    if json_schema_extra and "field_type" in json_schema_extra:
        field_type_value = json_schema_extra["field_type"]
        if not isinstance(field_type_value, InputFieldType):
            raise TypeError(
                f"field_type must be an InputFieldType enum, got {type(field_type_value).__name__}"
            )
        return field_type_value

    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        if type(None) in args:
            annotation = next(arg for arg in args if arg is not type(None))
            origin = get_origin(annotation)

    if origin is list:
        if json_schema_extra and "options" in json_schema_extra:
            return InputFieldType.MULTI_SELECT
        return InputFieldType.JSON

    type_map = {
        str: InputFieldType.STR,
        int: InputFieldType.INT,
        float: InputFieldType.FLOAT,
        bool: InputFieldType.BOOL,
        dict: InputFieldType.JSON,
        list: InputFieldType.JSON,
    }

    return type_map.get(annotation, InputFieldType.STR)


def config_from_pydantic(
    *,
    config_id: str,
    name: str,
    description: str,
    input_model: type[BaseModel],
) -> VerifierConfig:
    """Auto-generate VerifierConfig from Pydantic model with 1:1 field mapping."""
    input_fields: list[InputFieldDefinition] = []

    for field_name, field_info in input_model.model_fields.items():
        annotation = field_info.annotation
        is_required = field_info.is_required()
        default_value = field_info.default if not is_required else None

        # Get json_schema_extra (should be a dict, not callable)
        json_schema_extra: dict[str, Any] | None = None
        if field_info.json_schema_extra is not None and isinstance(
            field_info.json_schema_extra, dict
        ):
            json_schema_extra = field_info.json_schema_extra

        input_label_raw = (
            json_schema_extra.get("input_label", field_name.replace("_", " ").title())
            if json_schema_extra
            else field_name.replace("_", " ").title()
        )
        input_label = (
            input_label_raw
            if isinstance(input_label_raw, str)
            else str(input_label_raw)
        )

        input_subtitle_raw = (
            json_schema_extra.get("input_subtitle", None) if json_schema_extra else None
        )
        input_subtitle = (
            input_subtitle_raw
            if isinstance(input_subtitle_raw, (str, type(None)))
            else None
        )

        input_hidden_raw = (
            json_schema_extra.get("input_hidden", None) if json_schema_extra else None
        )
        input_hidden = input_hidden_raw if isinstance(input_hidden_raw, bool) else False

        options_raw = (
            json_schema_extra.get("options", None) if json_schema_extra else None
        )
        options: list[str] | None = None
        if options_raw is not None and isinstance(options_raw, list):
            # Ensure all items are strings
            options = [str(item) for item in options_raw]

        field_type = _infer_field_type(
            annotation=annotation,
            field_info=field_info,
            json_schema_extra=json_schema_extra,
        )

        input_fields.append(
            InputFieldDefinition(
                input_id=field_name,
                input_label=input_label,
                input_subtitle=input_subtitle,
                type=field_type,
                required=is_required,
                default_value=default_value,
                options=options,
                input_hidden=input_hidden,
            )
        )

    return VerifierConfig(
        id=config_id,
        name=name,
        description=description,
        input_fields=input_fields,
    )
