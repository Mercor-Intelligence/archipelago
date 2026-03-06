"""Gemini-compatible JSON Schema utilities.

The Gemini API's Function Calling feature requires a specific subset of JSON Schema.
It does NOT support:
- $defs / $ref (Pydantic nested model references)
- anyOf / oneOf / allOf (union patterns)
- default values
- title fields
- additionalProperties, format, pattern, min/max constraints, etc.

This module provides utilities to transform Pydantic v2 schemas into a flat format
that Gemini Function Calling accepts.

See: https://ai.google.dev/gemini-api/docs/structured-output
"""

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import GenerateJsonSchema

UNSUPPORTED_FIELDS = frozenset(
    [
        "$defs",
        "$ref",
        "default",
        "title",
        "additionalProperties",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "uniqueItems",
        "discriminator",
        "examples",
        "prefixItems",
    ]
)

# Plain pydantic BaseModel alias for MCP tool *output* schemas.
# GeminiBaseModel flattens ``str | None`` to ``{"type": "string", "nullable": true}``
# which the MCP SDK's ``jsonschema.validate()`` rejects (``nullable`` is not standard
# JSON Schema).  Output models must use vanilla BaseModel so ``str | None`` serialises
# as ``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` — the format jsonschema
# understands.
OutputBaseModel = BaseModel


def _infer_missing_type(schema: dict[str, Any]) -> str:
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if "enum" in schema:
        return "string"
    return "string"


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
            if "const" in obj and "enum" not in obj:
                obj = {**obj, "enum": [obj["const"]]}

            if "$defs" in obj:
                local_defs = {**(defs or {}), **obj["$defs"]}
            else:
                local_defs = defs

            # Handle $ref - inline the referenced definition
            ref = obj.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                ref_key = ref.split("/")[-1]
                sibling_props = {
                    k: inline_refs(v, local_defs, seen)
                    for k, v in obj.items()
                    if k not in UNSUPPORTED_FIELDS and k not in ("$ref", "anyOf", "oneOf", "allOf")
                }
                if local_defs and ref_key in local_defs:
                    if ref_key in seen:
                        return {
                            "type": "object",
                            "description": f"(recursive reference: {ref_key})",
                            **sibling_props,
                        }
                    inlined_def = inline_refs(
                        deepcopy(local_defs[ref_key]),
                        local_defs,
                        seen | {ref_key},
                    )
                    return {**inlined_def, **sibling_props}
                if "type" not in sibling_props:
                    sibling_props["type"] = "object"
                return sibling_props

            # Handle anyOf / oneOf / allOf - pick first non-null branch
            for union_key in ("anyOf", "oneOf", "allOf"):
                branches = obj.get(union_key)
                if not isinstance(branches, list) or len(branches) == 0:
                    continue
                non_null_types = [
                    item
                    for item in branches
                    if isinstance(item, dict) and item.get("type") != "null"
                ]

                if len(non_null_types) == 0:
                    result = {
                        k: inline_refs(v, local_defs, seen)
                        for k, v in obj.items()
                        if k not in UNSUPPORTED_FIELDS and k not in ("anyOf", "oneOf", "allOf")
                    }
                    if "type" not in result:
                        result["type"] = _infer_missing_type(result)
                    return result

                field_description = obj.get("description")
                result = {
                    k: inline_refs(v, local_defs, seen)
                    for k, v in obj.items()
                    if k not in UNSUPPORTED_FIELDS and k not in ("anyOf", "oneOf", "allOf")
                }
                if union_key == "allOf":
                    for branch in non_null_types:
                        inlined = inline_refs(branch, local_defs, seen)
                        if not isinstance(inlined, dict):
                            continue
                        branch_props = inlined.pop("properties", None)
                        branch_required = inlined.pop("required", None)
                        result.update(inlined)
                        if branch_props:
                            result.setdefault("properties", {}).update(branch_props)
                        if branch_required:
                            existing = set(result.get("required", []))
                            result["required"] = sorted(existing | set(branch_required))
                    if field_description is not None:
                        result["description"] = field_description
                else:
                    inlined_branches = [
                        inline_refs(branch, local_defs, seen) for branch in non_null_types
                    ]
                    can_merge_union_objects = len(non_null_types) > 1 and all(
                        isinstance(branch_schema, dict)
                        and isinstance(branch_schema.get("properties"), dict)
                        for branch_schema in inlined_branches
                    )
                    if can_merge_union_objects:
                        merged_properties = result.setdefault("properties", {})
                        required_sets: list[set[str]] = []
                        for branch_schema in inlined_branches:
                            assert isinstance(branch_schema, dict)
                            branch_props = branch_schema.get("properties", {})
                            assert isinstance(branch_props, dict)
                            for prop_name, prop_schema in branch_props.items():
                                existing_prop = merged_properties.get(prop_name)
                                if (
                                    isinstance(existing_prop, dict)
                                    and isinstance(prop_schema, dict)
                                    and isinstance(existing_prop.get("enum"), list)
                                    and isinstance(prop_schema.get("enum"), list)
                                ):
                                    merged_enum = list(existing_prop["enum"])
                                    for enum_value in prop_schema["enum"]:
                                        if enum_value not in merged_enum:
                                            merged_enum.append(enum_value)
                                    merged_prop = {**existing_prop, **prop_schema}
                                    merged_prop["enum"] = merged_enum
                                    merged_properties[prop_name] = merged_prop
                                else:
                                    merged_properties[prop_name] = prop_schema
                            branch_required = branch_schema.get("required")
                            if isinstance(branch_required, list):
                                required_sets.append(set(branch_required))
                            else:
                                required_sets.append(set())
                        if required_sets:
                            result["required"] = sorted(set.intersection(*required_sets))
                    else:
                        item = non_null_types[0]
                        result.update(inline_refs(item, local_defs, seen))

                    if len(non_null_types) > 1:
                        type_names = [
                            t.get("type", t.get("$ref", "unknown").split("/")[-1])
                            for t in non_null_types
                        ]
                        union_note = f"(Union of: {', '.join(type_names)})"
                        if field_description:
                            result["description"] = f"{field_description} {union_note}"
                        else:
                            result["description"] = union_note
                    elif field_description is not None:
                        result["description"] = field_description

                if "type" not in result:
                    result["type"] = _infer_missing_type(result)
                return result

            # Capture prefixItems before filtering (for tuple types)
            prefix_items = obj.get("prefixItems")

            # Recurse into children, dropping unsupported fields
            inlined: dict[str, Any] = {}
            for key, value in obj.items():
                if key in UNSUPPORTED_FIELDS:
                    continue
                if key == "properties" and isinstance(value, dict):
                    inlined[key] = {
                        prop_name: inline_refs(prop_schema, local_defs, seen)
                        for prop_name, prop_schema in value.items()
                    }
                else:
                    inlined[key] = inline_refs(value, local_defs, seen)

            # Ensure arrays have items (required by Gemini)
            if inlined.get("type") == "array" and "items" not in inlined:
                if isinstance(prefix_items, list) and len(prefix_items) > 0:
                    inlined["items"] = inline_refs(prefix_items[0], local_defs, seen)
                else:
                    inlined["items"] = {"type": "string"}

            return inlined

        if isinstance(obj, list):
            return [inline_refs(item, defs, seen) for item in obj]

        return obj

    flattened = inline_refs(schema)
    _ensure_types(flattened)
    _strip_docstring_attributes_from_descriptions(flattened)
    _annotate_optional_fields(flattened)
    return flattened


def _ensure_types(schema: dict[str, Any]) -> None:
    """Walk a flattened schema and guarantee every property/items node has ``type``."""
    if not isinstance(schema, dict):
        return

    if "type" not in schema:
        schema["type"] = _infer_missing_type(schema)

    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            _ensure_types(prop)

    items = schema.get("items")
    if isinstance(items, dict):
        if not items:
            schema["items"] = {"type": "string"}
        else:
            _ensure_types(items)
    if schema.get("type") == "object" and "properties" not in schema:
        schema["properties"] = {}


def _strip_docstring_attributes_from_descriptions(schema: dict[str, Any]) -> None:
    """Remove Attributes/Parameters sections from descriptions to avoid duplication.

    Pydantic uses class docstrings (including Attributes: blocks) as schema
    descriptions. When fields already have Field(description=...), this repeats
    the same info. We strip the Attributes/Parameters block so only the summary
    and field-level descriptions remain. Only strip when properties have their
    own descriptions to avoid losing info when Attributes is the sole source.
    """
    props = schema.get("properties", {})
    has_field_descriptions = any(
        isinstance(p, dict) and p.get("description") for p in props.values()
    )
    if has_field_descriptions:
        if "description" in schema and isinstance(schema["description"], str):
            desc = schema["description"]
            min_idx = -1
            for marker in ("\n\nAttributes:", "\n\nParameters:", "\n\nArgs:"):
                idx = desc.find(marker)
                if idx >= 0 and (min_idx < 0 or idx < min_idx):
                    min_idx = idx
            if min_idx >= 0:
                schema["description"] = desc[:min_idx].rstrip()
    for name, prop in props.items():
        if isinstance(prop, dict):
            _strip_docstring_attributes_from_descriptions(prop)
    items = schema.get("items")
    if isinstance(items, dict):
        _strip_docstring_attributes_from_descriptions(items)


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


def _schema_has_ref(node: Any) -> bool:
    """Return ``True`` if *node* contains any ``$ref`` key."""
    if isinstance(node, dict):
        if "$ref" in node:
            return True
        return any(_schema_has_ref(v) for v in node.values())
    if isinstance(node, list):
        return any(_schema_has_ref(item) for item in node)
    return False


def _flatten_schema_inplace(schema: dict[str, Any], _cls: type | None = None) -> None:
    """``json_schema_extra`` callback that flattens *in-place*.

    Pydantic's ``json_schema_extra`` callable receives ``(schema, cls)`` and
    must **mutate** the schema dict — its return value is ignored.  We delegate
    to ``flatten_schema`` (which returns a new dict) and then swap the contents
    of the original dict so callers see the flattened version.

    When this callback fires on a nested model inside another schema's
    ``$defs``, the local schema has ``$ref`` but no ``$defs`` of its own,
    so those refs are unresolvable.  We skip and let the top-level caller
    handle it with full ``$defs`` context.
    """
    if "$defs" not in schema and _schema_has_ref(schema):
        return
    flattened = flatten_schema(schema)
    schema.clear()
    schema.update(flattened)


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
        json_schema_extra=_flatten_schema_inplace ensures that schemas produced via
        TypeAdapter.json_schema() (e.g. FastMCP) are fully flattened and annotated
        in one pass. The model_json_schema() / get_gemini_schema() path also calls
        flatten_schema via GeminiSchemaGenerator — re-flattening an already-flat
        schema is a no-op, so the double call is safe.
    """

    model_config = ConfigDict(json_schema_extra=_flatten_schema_inplace)

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
