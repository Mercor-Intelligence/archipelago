"""Flatten JSON schemas for LLM Function Calling compatibility."""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict

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
        "examples",
        "prefixItems",
        "discriminator",
    ]
)


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten a JSON schema by inlining $ref and removing unsupported fields."""

    def get_type_name(t: dict[str, Any]) -> str:
        if "type" in t:
            return str(t["type"])
        ref_val = t.get("$ref")
        if isinstance(ref_val, str) and "/" in ref_val:
            return ref_val.split("/")[-1]
        return "unknown"

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
                            "description": f"(recursive: {ref_key})",
                            **sibling_props,
                        }
                    inlined_def = inline_refs(
                        deepcopy(local_defs[ref_key]), local_defs, seen | {ref_key}
                    )
                    return {**inlined_def, **sibling_props}
                if "type" not in sibling_props:
                    sibling_props["type"] = "object"
                return sibling_props

            for union_key in ("anyOf", "oneOf", "allOf"):
                branches = obj.get(union_key)
                if not isinstance(branches, list) or len(branches) == 0:
                    continue
                non_null_types = [
                    item
                    for item in branches
                    if isinstance(item, dict) and item.get("type") != "null"
                ]

                has_null = len(non_null_types) < len([b for b in branches if isinstance(b, dict)])

                if len(non_null_types) == 0:
                    result = {
                        k: inline_refs(v, local_defs, seen)
                        for k, v in obj.items()
                        if k not in UNSUPPORTED_FIELDS and k not in ("anyOf", "oneOf", "allOf")
                    }
                    if "type" not in result:
                        result["type"] = _infer_missing_type(result)
                    result["nullable"] = True
                    return result

                field_description = obj.get("description")
                union_note = None
                if len(non_null_types) > 1:
                    type_names = [get_type_name(t) for t in non_null_types]
                    union_note = f"(Union of: {', '.join(type_names)})"
                result = {
                    k: inline_refs(v, local_defs, seen)
                    for k, v in obj.items()
                    if k not in UNSUPPORTED_FIELDS and k not in ("anyOf", "oneOf", "allOf")
                }

                if union_key == "allOf":
                    item = non_null_types[0]
                    result.update(inline_refs(item, local_defs, seen))
                else:
                    merged_union = False
                    if len(non_null_types) > 1:
                        inlined_branches = [
                            inline_refs(branch, local_defs, seen) for branch in non_null_types
                        ]
                        if all(
                            isinstance(branch, dict) and isinstance(branch.get("properties"), dict)
                            for branch in inlined_branches
                        ):
                            merged_props = dict(result.get("properties", {}))
                            required_sets: list[set[str]] = []
                            for branch in inlined_branches:
                                assert isinstance(branch, dict)
                                for prop_name, prop_schema in branch["properties"].items():
                                    if (
                                        prop_name in merged_props
                                        and isinstance(merged_props[prop_name], dict)
                                        and isinstance(prop_schema, dict)
                                    ):
                                        existing_enum = merged_props[prop_name].get("enum")
                                        new_enum = prop_schema.get("enum")
                                        if isinstance(existing_enum, list) and isinstance(
                                            new_enum, list
                                        ):
                                            seen_enum_values: set[Any] = set()
                                            merged_enum: list[Any] = []
                                            for enum_val in [*existing_enum, *new_enum]:
                                                if enum_val in seen_enum_values:
                                                    continue
                                                seen_enum_values.add(enum_val)
                                                merged_enum.append(enum_val)
                                            merged_props[prop_name] = {
                                                **merged_props[prop_name],
                                                **prop_schema,
                                                "enum": merged_enum,
                                            }
                                            continue
                                    merged_props[prop_name] = prop_schema
                                branch_required = branch.get("required")
                                if isinstance(branch_required, list):
                                    required_sets.append(set(branch_required))
                                else:
                                    required_sets.append(set())
                            result["properties"] = merged_props
                            if required_sets:
                                shared_required = set.intersection(*required_sets)
                                result["required"] = sorted(shared_required)
                            merged_union = True
                    if not merged_union:
                        item = non_null_types[0]
                        result.update(inline_refs(item, local_defs, seen))

                if union_note:
                    if field_description:
                        result["description"] = f"{field_description} {union_note}"
                    else:
                        result["description"] = union_note
                elif field_description is not None:
                    result["description"] = field_description

                if has_null:
                    result["nullable"] = True

                if "type" not in result:
                    result["type"] = _infer_missing_type(result)
                return result

            # Capture prefixItems before filtering (for tuple types)
            prefix_items = obj.get("prefixItems")

            inlined: dict[str, Any] = {}
            for key, value in obj.items():
                if key in UNSUPPORTED_FIELDS:
                    continue
                if key == "properties" and isinstance(value, dict):
                    # Don't filter property names - only recurse into their schemas
                    inlined[key] = {
                        prop_name: inline_refs(prop_schema, local_defs, seen)
                        for prop_name, prop_schema in value.items()
                    }
                else:
                    inlined[key] = inline_refs(value, local_defs, seen)

            # Ensure arrays have items (required by Gemini)
            if inlined.get("type") == "array" and "items" not in inlined:
                # Try to infer type from prefixItems (tuple types)
                if isinstance(prefix_items, list) and len(prefix_items) > 0:
                    # Use inline_refs to properly flatten nested structures
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


def _infer_missing_type(schema: dict[str, Any]) -> str:
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if "enum" in schema:
        return "string"
    return "string"


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
    """Add an ``(Optional)`` description prefix to non-required properties.

    After flattening strips ``anyOf`` and ``default``, the only signal that a
    field is optional is its absence from the ``required`` array.  LLMs
    frequently overlook this, so we add an ``"(Optional) "`` prefix in the
    description as a natural-language fallback.

    ``nullable: true`` is set separately during ``anyOf`` resolution — only
    for fields whose type actually includes ``null`` — so we don't touch it
    here.  This avoids conflating "has a default" with "accepts None".
    """
    required = set(schema.get("required", []))
    for name, prop in schema.get("properties", {}).items():
        if not isinstance(prop, dict):
            continue
        if name not in required:
            desc = prop.get("description", "")
            if not desc.startswith("(Optional)"):
                prop["description"] = f"(Optional) {desc}" if desc else "(Optional)"
        if prop.get("type") == "object" and "properties" in prop:
            _annotate_optional_fields(prop)
        elif prop.get("type") == "array" and isinstance(prop.get("items"), dict):
            items = prop["items"]
            if items.get("type") == "object" and "properties" in items:
                _annotate_optional_fields(items)


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
    ``$defs``, the local schema has no ``$defs`` of its own, so any ``$ref``
    targets are unresolvable.  Attempting to flatten in that situation strips
    the ``$ref`` (an unsupported field) and loses type information.  We skip
    flattening here and let the top-level caller (``apply_default_setup`` or
    ``flatten_schema``) handle it with full ``$defs`` context.
    """
    if "$defs" not in schema and _schema_has_ref(schema):
        return
    flattened = flatten_schema(schema)
    schema.clear()
    schema.update(flattened)


class FlatBaseModel(BaseModel):
    """BaseModel subclass that generates flattened JSON schemas.

    Use this instead of BaseModel for models that need LLM-compatible schemas.
    """

    model_config = ConfigDict(json_schema_extra=_flatten_schema_inplace)

    @classmethod
    def model_json_schema(cls, **kwargs: Any) -> dict[str, Any]:
        """Generate a flattened JSON schema.

        Flattening is handled by the ``json_schema_extra`` callback
        (``_flatten_schema_inplace``) which runs inside
        ``super().model_json_schema()``, so no extra wrapping is needed.
        """
        return super().model_json_schema(**kwargs)


OutputBaseModel = BaseModel
