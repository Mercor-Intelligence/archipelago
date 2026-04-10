"""Schema utilities for handling JSON schemas with Gemini compatibility.

Gemini does not support $defs or $ref in tool schemas, so we need to
flatten/resolve these references before sending schemas to the LLM.
"""

from typing import Any


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten JSON schema by resolving $ref and removing $defs.

    This is necessary for Gemini compatibility, as Gemini rejects schemas
    with $defs/$ref in function responses with error:
    "The referenced name `#/$defs/X` does not match to a display_name"

    Args:
        schema: JSON schema that may contain $defs and $ref

    Returns:
        Flattened schema with all references resolved inline
    """
    if not isinstance(schema, dict):
        return schema

    # Extract definitions
    defs = schema.pop("$defs", {})

    def resolve_refs(obj: Any, visiting: set[str] | None = None) -> Any:
        """
        Recursively resolve all $ref references.

        Args:
            obj: The object to resolve references in
            visiting: Set of definition names currently being resolved (for cycle detection)
        """
        if visiting is None:
            visiting = set()

        if not isinstance(obj, dict):
            return obj

        # If this is a reference, resolve it
        if "$ref" in obj:
            ref_path = obj["$ref"]
            # Handle references like "#/$defs/HistoricalPriceRequest"
            if ref_path.startswith("#/$defs/"):
                def_name = ref_path.split("/")[-1]

                # Check for circular reference
                if def_name in visiting:
                    # Circular reference detected - break the cycle
                    # Return a simple object schema to prevent infinite recursion
                    result = {"type": "object"}

                    # Preserve sibling properties alongside $ref
                    for key, value in obj.items():
                        if key != "$ref":
                            result[key] = value

                    return result

                if def_name in defs:
                    # Mark this definition as being visited
                    visiting.add(def_name)

                    try:
                        # Recursively resolve the definition
                        resolved = resolve_refs(defs[def_name].copy(), visiting)

                        # Preserve sibling properties alongside $ref
                        # These are valid JSON Schema metadata (description, default, title, etc.)
                        for key, value in obj.items():
                            if key != "$ref":
                                # Sibling props take precedence (they override the resolved def)
                                resolved[key] = value

                        return resolved
                    finally:
                        # Remove from visiting set after resolution
                        visiting.discard(def_name)
            # If we can't resolve, return a placeholder object
            # but preserve sibling properties (description, default, title, etc.)
            result = {"type": "object"}
            for key, value in obj.items():
                if key != "$ref":
                    result[key] = value
            return result

        # Recursively process all nested objects
        result = {}
        for key, value in obj.items():
            if isinstance(value, dict):
                result[key] = resolve_refs(value, visiting)
            elif isinstance(value, list):
                result[key] = [
                    resolve_refs(item, visiting) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value

        return result

    return resolve_refs(schema)


def sanitize_tool_schema(tool_definition: dict[str, Any]) -> dict[str, Any]:
    """
    Sanitize a tool definition to be Gemini-compatible.

    Flattens the parameters schema by removing $defs and resolving $ref.

    Args:
        tool_definition: Tool definition dict with 'function' key containing parameters

    Returns:
        Sanitized tool definition
    """
    if "function" not in tool_definition:
        return tool_definition

    func = tool_definition["function"]
    if "parameters" in func and isinstance(func["parameters"], dict):
        func["parameters"] = flatten_schema(func["parameters"].copy())

    return tool_definition
