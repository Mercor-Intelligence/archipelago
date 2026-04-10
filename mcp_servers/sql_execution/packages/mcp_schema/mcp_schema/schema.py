"""Flatten JSON schemas for LLM Function Calling compatibility."""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel

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
            if "$defs" in obj:
                local_defs = {**(defs or {}), **obj["$defs"]}
            else:
                local_defs = defs

            ref = obj.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/") and local_defs:
                ref_key = ref.split("/")[-1]
                if ref_key in local_defs:
                    sibling_props = {
                        k: inline_refs(v, local_defs, seen)
                        for k, v in obj.items()
                        if k not in UNSUPPORTED_FIELDS and k not in ("$ref", "anyOf")
                    }
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

            any_of = obj.get("anyOf")
            if isinstance(any_of, list) and len(any_of) > 0:
                non_null_types = [
                    item for item in any_of if isinstance(item, dict) and item.get("type") != "null"
                ]

                if len(non_null_types) == 0:
                    result = {
                        k: inline_refs(v, local_defs, seen)
                        for k, v in obj.items()
                        if k not in UNSUPPORTED_FIELDS and k != "anyOf"
                    }
                    if "type" not in result:
                        result["type"] = "string"
                    return result

                item = non_null_types[0]
                field_description = obj.get("description")
                result = {
                    k: inline_refs(v, local_defs, seen)
                    for k, v in obj.items()
                    if k not in UNSUPPORTED_FIELDS and k != "anyOf"
                }
                result.update(inline_refs(item, local_defs, seen))

                if len(non_null_types) > 1:
                    type_names = [get_type_name(t) for t in non_null_types]
                    union_note = f"(Union of: {', '.join(type_names)})"
                    if field_description:
                        result["description"] = f"{field_description} {union_note}"
                    else:
                        result["description"] = union_note
                elif field_description is not None:
                    result["description"] = field_description

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

    return inline_refs(schema)


class FlatBaseModel(BaseModel):
    """BaseModel subclass that generates flattened JSON schemas.

    Use this instead of BaseModel for models that need LLM-compatible schemas.
    """

    @classmethod
    def model_json_schema(cls, **kwargs: Any) -> dict[str, Any]:
        """Generate a flattened JSON schema."""
        return flatten_schema(super().model_json_schema(**kwargs))
