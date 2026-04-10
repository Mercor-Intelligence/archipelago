"""Gemini-compatible JSON Schema utilities.

The Gemini API's Function Calling feature requires a specific subset of JSON Schema.
It does NOT support:
- $defs / $ref (Pydantic nested model references)
- anyOf (Pydantic Optional[X] / X | None patterns)
- default values
- title fields

This module provides utilities to transform Pydantic v2 schemas into a flat format
that Gemini Function Calling accepts.

See: https://ai.google.dev/gemini-api/docs/structured-output
"""

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import GenerateJsonSchema


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Pydantic JSON schema for Gemini Function Calling compatibility.

    This function:
    - Inlines all $ref references (removes $defs)
    - Converts anyOf patterns to simple types (handles Optional[X])
    - Removes unsupported fields (default, title)

    Args:
        schema: A JSON schema (typically from model_json_schema())

    Returns:
        A flattened schema without $defs, $ref, or anyOf

    Example:
        >>> from pydantic import BaseModel
        >>> class MyInput(BaseModel):
        ...     name: str
        ...     value: int | None = None
        >>> schema = flatten_schema(MyInput.model_json_schema())
        >>> "$defs" in str(schema)
        False
        >>> "anyOf" in str(schema)
        False
    """

    def inline_refs(
        obj: Any,
        defs: dict[str, Any] | None = None,
        seen: set[str] | None = None,
    ) -> Any:
        if seen is None:
            seen = set()

        if isinstance(obj, dict):
            # Get definitions from current level or use passed-in defs
            local_defs = obj.get("$defs", defs)

            # Handle $ref - inline the referenced definition
            ref = obj.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/") and local_defs:
                ref_key = ref.split("/")[-1]
                if ref_key in local_defs:
                    if ref_key in seen:
                        # Recursive reference - return a generic object to prevent infinite loop
                        return {
                            "type": "object",
                            "description": f"(recursive reference: {ref_key})",
                        }
                    return inline_refs(
                        deepcopy(local_defs[ref_key]),
                        local_defs,
                        seen | {ref_key},
                    )

            # Handle anyOf - extract non-null types (Pydantic's Optional pattern)
            # For Union[A, B, None], Gemini doesn't support anyOf so we need to pick
            # the most appropriate type. For multiple non-null types, we note the
            # limitation in the description.
            any_of = obj.get("anyOf")
            if isinstance(any_of, list) and len(any_of) > 0:
                # Collect all non-null types
                non_null_types = [
                    item for item in any_of if isinstance(item, dict) and item.get("type") != "null"
                ]

                if len(non_null_types) == 1:
                    # Simple Optional[X] pattern - use the single non-null type
                    item = non_null_types[0]
                    field_description = obj.get("description")
                    result = {
                        k: v
                        for k, v in obj.items()
                        if k not in ("anyOf", "$defs", "default")
                        and not (k == "title" and isinstance(v, str))
                    }
                    result.update(inline_refs(item, local_defs, seen))
                    # Restore field-level description if present
                    if field_description is not None:
                        result["description"] = field_description
                    return result
                elif len(non_null_types) > 1:
                    # Multiple non-null types (e.g., str | int | None or str | int)
                    # Gemini doesn't support union types, so use the first type
                    # and document the limitation
                    item = non_null_types[0]
                    field_description = obj.get("description")
                    result = {
                        k: v
                        for k, v in obj.items()
                        if k not in ("anyOf", "$defs", "default")
                        and not (k == "title" and isinstance(v, str))
                    }
                    result.update(inline_refs(item, local_defs, seen))
                    # Build union type note
                    type_names = [
                        t.get("type", t.get("$ref", "unknown").split("/")[-1])
                        for t in non_null_types
                    ]
                    union_note = f"(Union of: {', '.join(type_names)})"
                    if field_description:
                        result["description"] = f"{field_description} {union_note}"
                    else:
                        result["description"] = union_note
                    return result

            # Recurse into children, dropping unsupported fields
            inlined: dict[str, Any] = {}
            for key, value in obj.items():
                if key in ("$defs", "default"):
                    continue
                # Only remove "title" when it's JSON Schema metadata (string value),
                # not when it's a property name (dict value) in the properties object
                if key == "title" and isinstance(value, str):
                    continue
                inlined[key] = inline_refs(value, local_defs, seen)
            return inlined

        if isinstance(obj, list):
            return [inline_refs(item, defs, seen) for item in obj]

        return obj

    flattened = inline_refs(schema)
    _annotate_optional_fields(flattened)
    return flattened


def _annotate_optional_fields(schema: dict[str, Any]) -> None:
    """Mark non-required properties with ``nullable: true`` and an (Optional) prefix.

    After flattening strips ``anyOf`` and ``default``, the only signal that a
    field is optional is its absence from the ``required`` array.  LLMs
    frequently overlook this, so we add two redundant hints:

    1. ``"nullable": true`` — a structured flag Gemini understands.
    2. ``"(Optional) "`` prefix in the description — a natural-language fallback.

    Recurses into nested objects and array item schemas so optional fields
    are annotated throughout (e.g. items: list[SomeModel]).
    """
    required = set(schema.get("required", []))
    for name, prop in schema.get("properties", {}).items():
        if not isinstance(prop, dict):
            continue
        if name not in required:
            prop["nullable"] = True
            desc = prop.get("description", "")
            if not desc.startswith("(Optional)"):
                prop["description"] = f"(Optional) {desc}" if desc else "(Optional)"
        if prop.get("type") == "object" and "properties" in prop:
            _annotate_optional_fields(prop)
        elif prop.get("type") == "array" and isinstance(prop.get("items"), dict):
            items = prop["items"]
            if items.get("type") == "object" and "properties" in items:
                _annotate_optional_fields(items)


class GeminiSchemaGenerator(GenerateJsonSchema):
    """Custom Pydantic schema generator that produces Gemini-compatible schemas.

    This generator wraps Pydantic's default JSON schema generation and
    post-processes the output to remove unsupported constructs.

    Usage:
        >>> from pydantic import BaseModel
        >>> class MyInput(BaseModel):
        ...     name: str
        ...     value: int | None = None
        >>> schema = MyInput.model_json_schema(schema_generator=GeminiSchemaGenerator)
        >>> "$defs" in str(schema)
        False
    """

    def generate(self, schema, mode: str = "validation"):
        """Generate a Gemini-compatible JSON schema."""
        json_schema = super().generate(schema, mode)
        return flatten_schema(json_schema)


def get_gemini_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Get a Gemini-compatible JSON schema for a Pydantic model.

    This is a convenience function that calls model_json_schema with
    the GeminiSchemaGenerator.

    Args:
        model: A Pydantic BaseModel class

    Returns:
        A flattened JSON schema compatible with Gemini Function Calling

    Example:
        >>> from pydantic import BaseModel
        >>> class MyInput(BaseModel):
        ...     name: str
        >>> schema = get_gemini_schema(MyInput)
        >>> schema["properties"]["name"]["type"]
        'string'
    """
    return model.model_json_schema(schema_generator=GeminiSchemaGenerator)


class GeminiBaseModel(BaseModel):
    """Base model that generates Gemini-compatible JSON schemas.

    Inherit from this class instead of BaseModel to automatically get
    Gemini-compatible schemas from model_json_schema().

    This is the recommended approach for MCP tool input models that need
    to work with Gemini's Function Calling API.

    Usage:
        >>> class MyInput(GeminiBaseModel):
        ...     action: str
        ...     file_path: str | None = None
        ...
        >>> schema = MyInput.model_json_schema()
        >>> "$defs" in str(schema)
        False
        >>> "anyOf" in str(schema)
        False

    Note:
        This only affects schema generation. Model validation and serialization
        work exactly the same as regular Pydantic models.

    Note (annotation paths):
        _annotate_optional_fields runs in two places: (1) json_schema_extra here,
        for schemas produced via TypeAdapter.json_schema() (e.g. FastMCP); (2) inside
        flatten_schema(), for model_json_schema() / get_gemini_schema(). Both are
        needed because different code paths produce schemas. The double call is
        idempotent.
    """

    model_config = ConfigDict(json_schema_extra=_annotate_optional_fields)

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = GeminiSchemaGenerator,
        mode: Literal["validation", "serialization"] = "serialization",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        """Generate a Gemini-compatible JSON schema for this model.

        This overrides the default Pydantic method to use GeminiSchemaGenerator
        by default, producing flat schemas without $defs, $ref, or anyOf.

        Args:
            by_alias: Whether to use field aliases in the schema
            ref_template: Template for $ref URLs (ignored by GeminiSchemaGenerator)
            schema_generator: The schema generator class to use
            mode: Schema mode ('validation' or 'serialization')
            union_format: Format for union types ('any_of' or 'primitive_type_array')

        Returns:
            A Gemini-compatible JSON schema
        """
        return super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
