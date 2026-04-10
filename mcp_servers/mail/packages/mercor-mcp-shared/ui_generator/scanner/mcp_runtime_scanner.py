"""
MCP Runtime Scanner - Discover tools by importing the MCP server and getting schemas from FastMCP.

This scanner uses a direct approach:
1. Import main module to trigger tool registration (respects GUI_ENABLED)
2. Get the list of registered tools with their schemas from FastMCP
3. Convert schemas to UI generator format

This is fast (no subprocess or module scanning) and accurate (uses exactly what's registered).
"""

import importlib
import inspect
import os
import sys
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel


class MCPRuntimeScanner:
    """Scan MCP servers using FastMCP's registered tools and schemas."""

    def _get_schema_from_type_hints(self, fn: Any) -> dict[str, Any] | None:
        """
        Generate JSON schema from function type hints using standard Pydantic.

        This bypasses any Gemini schema flattening to preserve $defs and nested models.
        Returns None if schema generation fails (falls back to FastMCP schema).
        """
        try:
            # Use include_extras=True to preserve Annotated metadata (Field descriptions, etc.)
            hints = get_type_hints(fn, include_extras=True)
            if not hints:
                return None

            # Build schema from type hints
            properties: dict[str, Any] = {}
            required: list[str] = []
            defs: dict[str, Any] = {}

            # Get function signature for defaults
            sig = inspect.signature(fn)

            for param_name, param_type in hints.items():
                if param_name == "return":
                    continue

                # Check if parameter has a default
                param_info = sig.parameters.get(param_name)
                has_default = (
                    param_info is not None and param_info.default is not inspect.Parameter.empty
                )
                if not has_default:
                    required.append(param_name)

                # Generate schema for this parameter type
                param_schema = self._type_to_schema(param_type, defs)

                # Include default value in schema if present (and not from Annotated Field)
                # Annotated Field defaults are already handled in _type_to_schema
                if has_default and "default" not in param_schema:
                    default_val = param_info.default
                    # Only include JSON-serializable defaults
                    if isinstance(default_val, str | int | float | bool | list | dict | type(None)):
                        param_schema["default"] = default_val

                properties[param_name] = param_schema

            schema: dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "required": required,
            }
            if defs:
                schema["$defs"] = defs

            return schema
        except Exception as e:
            print(f"    Warning: Could not generate schema from type hints: {e}")
            return None

    def _type_to_schema(self, typ: Any, defs: dict[str, Any]) -> dict[str, Any]:
        """Convert a Python type to JSON schema, collecting model definitions."""
        origin = get_origin(typ)
        args = get_args(typ)

        # Handle None type
        if typ is type(None):
            return {"type": "null"}

        # Handle Optional (Union with None)
        if origin is type(None) or (hasattr(typ, "__origin__") and typ.__origin__ is type(None)):
            return {"type": "null"}

        # Handle Union types (including Optional)
        # Check both typing.Union and types.UnionType (Python 3.10+ X | Y syntax)
        if origin is Union or origin is types.UnionType or isinstance(typ, types.UnionType):
            # Check if it's Optional (Union with None)
            non_none_args = [a for a in args if a is not type(None)]
            has_none = len(non_none_args) < len(args)

            if len(non_none_args) == 1:
                # Simple Optional[X]
                inner_schema = self._type_to_schema(non_none_args[0], defs)
                if has_none:
                    return {"anyOf": [inner_schema, {"type": "null"}]}
                return inner_schema
            else:
                # Union of multiple types
                schemas = [self._type_to_schema(a, defs) for a in non_none_args]
                if has_none:
                    schemas.append({"type": "null"})
                return {"anyOf": schemas}

        # Handle list types
        if origin is list:
            if args:
                arg = args[0]
                # list[Any] - treat as list of strings
                if arg is Any:
                    return {"type": "array", "items": {"type": "string"}}
                item_schema = self._type_to_schema(arg, defs)
                return {"type": "array", "items": item_schema}
            # Raw list without args - treat as list of strings
            return {"type": "array", "items": {"type": "string"}}

        # Handle dict types
        if origin is dict:
            return {"type": "object"}

        # Handle Pydantic models - use standard schema generation (not Gemini)
        if isinstance(typ, type) and issubclass(typ, BaseModel):
            model_name = typ.__name__
            if model_name not in defs:
                # Use BaseModel's schema generation to avoid Gemini flattening
                model_schema = BaseModel.model_json_schema.__func__(typ)
                # Extract the model definition (may have nested $defs)
                if "$defs" in model_schema:
                    defs.update(model_schema["$defs"])
                # Store the main definition
                model_def = {k: v for k, v in model_schema.items() if k != "$defs"}
                defs[model_name] = model_def
            return {"$ref": f"#/$defs/{model_name}"}

        # Handle Annotated types - extract Field metadata
        if hasattr(typ, "__metadata__"):
            # Get the base type from Annotated
            base_type = get_args(typ)[0] if get_args(typ) else typ
            schema = self._type_to_schema(base_type, defs)

            # Extract Field metadata (description, etc.) from __metadata__
            for metadata in typ.__metadata__:
                if hasattr(metadata, "description") and metadata.description:
                    schema["description"] = metadata.description
                if hasattr(metadata, "default") and metadata.default is not None:
                    # Handle PydanticUndefined
                    default = metadata.default
                    if not str(type(default).__name__).startswith("Pydantic"):
                        schema["default"] = default

            return schema

        # Handle basic types
        type_map = {
            str: {"type": "string"},
            int: {"type": "integer"},
            float: {"type": "number"},
            bool: {"type": "boolean"},
            type(None): {"type": "null"},
        }

        if typ in type_map:
            return type_map[typ]

        # Handle Any
        if typ is Any:
            return {}

        # Default to object
        return {"type": "object"}

    def _convert_def_to_model(
        self, model_name: str, model_def: dict[str, Any], defs: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Convert a JSON Schema $def to the model format expected by the UI generator.

        Args:
            model_name: Name of the model (from $defs key)
            model_def: JSON Schema definition of the model
            defs: All $defs for resolving nested references

        Returns:
            Model dict in format: {name, docstring, fields, bases, is_enum}
        """
        # Check if this is an enum type (has top-level enum array, no properties)
        if "enum" in model_def and "properties" not in model_def:
            # This is an enum - create fields from enum values
            fields = {}
            for enum_value in model_def["enum"]:
                fields[str(enum_value)] = {
                    "type": model_def.get("type", "string"),
                    "required": False,
                    "description": "",
                    "default": None,
                }
            return {
                "name": model_name,
                "docstring": model_def.get("description", ""),
                "fields": fields,
                "bases": [],
                "is_enum": True,
            }

        # Regular model with properties
        fields = {}
        properties = model_def.get("properties", {})
        required_set = set(model_def.get("required", []))

        for field_name, field_info in properties.items():
            field_data = self._convert_model_field_to_ui(
                field_info, field_name in required_set, defs
            )
            fields[field_name] = field_data

        return {
            "name": model_name,
            "docstring": model_def.get("description", ""),
            "fields": fields,
            "bases": [],
            "is_enum": False,
        }

    def _convert_model_field_to_ui(
        self, field_info: dict[str, Any], is_required: bool, defs: dict[str, Any]
    ) -> dict[str, Any]:
        """Convert a JSON Schema field to UI-compatible format for model fields."""
        field_data: dict[str, Any] = {
            "required": is_required,
            "description": field_info.get("description", ""),
            "default": field_info.get("default"),
        }

        # Handle anyOf (Optional types)
        actual_info = field_info
        if "anyOf" in field_info:
            # Find the non-null type
            for option in field_info["anyOf"]:
                if option.get("type") != "null":
                    actual_info = option
                    break
            # Extract enum from non-null option
            for option in field_info["anyOf"]:
                if option.get("type") != "null" and "enum" in option:
                    field_data["enum"] = option["enum"]
                    break

        # Handle $ref
        if "$ref" in actual_info:
            ref = actual_info["$ref"]
            if ref.startswith("#/$defs/"):
                field_data["type"] = "object"
                field_data["modelRef"] = ref[8:]
            else:
                field_data["type"] = "object"
            return field_data

        # Handle arrays
        if actual_info.get("type") == "array":
            items = actual_info.get("items", {})
            field_data["isList"] = True

            # Handle anyOf in items first (e.g., list[MyModel | None])
            if "anyOf" in items:
                for option in items["anyOf"]:
                    if option.get("type") != "null":
                        items = option  # Use the non-null option
                        break

            if "$ref" in items:
                ref = items["$ref"]
                field_data["type"] = "object"
                if ref.startswith("#/$defs/"):
                    field_data["modelRef"] = ref[8:]
            elif items.get("type") == "array":
                # Nested array (list[list[...]]) - render as JSON textarea
                field_data["type"] = "object"
            elif not items or items == {}:
                # Empty items means list[Any] - render as JSON textarea
                field_data["type"] = "object"
            else:
                item_type = items.get("type", "string")
                type_mapping = {
                    "string": "string",
                    "integer": "number",
                    "number": "number",
                    "boolean": "boolean",
                    "object": "object",
                }
                field_data["type"] = type_mapping.get(item_type, "string")

            return field_data

        # Handle basic types
        json_type = actual_info.get("type", "string")
        type_mapping = {
            "string": "string",
            "integer": "number",
            "number": "number",
            "boolean": "boolean",
            "object": "object",
        }
        field_data["type"] = type_mapping.get(json_type, "string")

        # Extract enum values if present
        if "enum" in actual_info:
            field_data["enum"] = actual_info["enum"]
            field_data["type"] = "string"

        return field_data

    def _collect_referenced_models(
        self, defs: dict[str, Any], referenced_names: set[str]
    ) -> list[dict[str, Any]]:
        """
        Collect all referenced models from $defs.

        Args:
            defs: The $defs dictionary from JSON schema
            referenced_names: Set of model names that are actually referenced

        Returns:
            List of model dicts in UI generator format
        """
        models = []
        for model_name in referenced_names:
            if model_name in defs:
                model = self._convert_def_to_model(model_name, defs[model_name], defs)
                models.append(model)
        return models

    def _find_referenced_models(
        self, schema: dict[str, Any], defs: dict[str, Any], found: set[str] | None = None
    ) -> set[str]:
        """
        Recursively find all model names referenced via $ref in a schema.

        Args:
            schema: JSON schema to search
            defs: All $defs for resolving nested references
            found: Set to accumulate found model names

        Returns:
            Set of referenced model names
        """
        if found is None:
            found = set()

        if isinstance(schema, dict):
            # Check for $ref
            if "$ref" in schema:
                ref = schema["$ref"]
                if ref.startswith("#/$defs/"):
                    model_name = ref[8:]
                    if model_name not in found:
                        found.add(model_name)
                        # Recursively check this model's definition for more refs
                        if model_name in defs:
                            self._find_referenced_models(defs[model_name], defs, found)

            # Recurse into all dict values
            for value in schema.values():
                self._find_referenced_models(value, defs, found)

        elif isinstance(schema, list):
            for item in schema:
                self._find_referenced_models(item, defs, found)

        return found

    def _resolve_ref(self, ref: str, defs: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve a $ref to its definition."""
        # Format: "#/$defs/ModelName"
        if ref.startswith("#/$defs/"):
            model_name = ref[8:]  # Remove "#/$defs/" prefix
            return defs.get(model_name)
        return None

    def _extract_properties(
        self, json_schema: dict[str, Any]
    ) -> tuple[dict[str, Any], set[str], dict[str, Any]]:
        """
        Extract properties and required fields from a JSON schema.

        Handles both direct properties and $ref references to $defs.
        For Pydantic model inputs, flattens the nested structure.

        Returns:
            Tuple of (properties, required_fields, defs)
        """
        defs = json_schema.get("$defs", {})
        properties = json_schema.get("properties", {})
        required_fields = set(json_schema.get("required", []))

        # Check if this is a Pydantic model wrapper (one property with $ref,
        # possibly accompanied by extra injected properties like page_number).
        # Also handles Pydantic v2's allOf wrapper pattern where $ref is nested
        # inside allOf (e.g. {"allOf": [{"$ref": "#/$defs/MyModel"}]}).
        ref_prop = None
        extra_props = {}
        for pname, pinfo in properties.items():
            ref_val = pinfo.get("$ref")
            if not ref_val and "allOf" in pinfo:
                for item in pinfo["allOf"]:
                    if "$ref" in item:
                        ref_val = item["$ref"]
                        break
            if ref_val and ref_prop is None:
                ref_prop = (pname, {"$ref": ref_val, **pinfo})
            else:
                extra_props[pname] = pinfo

        if ref_prop is not None and len(properties) - len(extra_props) == 1:
            resolved = self._resolve_ref(ref_prop[1]["$ref"], defs)
            if resolved:
                merged = {**resolved.get("properties", {}), **extra_props}
                return (
                    merged,
                    set(resolved.get("required", [])),
                    defs,
                )

        return properties, required_fields, defs

    def _convert_field(
        self,
        field_name: str,
        field_info: dict[str, Any],
        required_fields: set[str],
        defs: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Convert a single JSON Schema field to UI generator format.

        Handles:
        - Basic types (string, integer, number, boolean)
        - Enums (Literal types)
        - Arrays (list types) with item type resolution
        - Nested objects ($ref to other models)
        - anyOf/oneOf (Optional types)
        - All validators (min, max, minLength, maxLength, pattern)
        - Metadata (description, default, examples, format)
        """
        field_schema: dict[str, Any] = {
            "name": field_name,
            "required": field_name in required_fields,
        }

        # Handle anyOf (typically Optional[X] which becomes anyOf: [X, null])
        if "anyOf" in field_info:
            # Find the non-null type
            for option in field_info["anyOf"]:
                if option.get("type") != "null":
                    # Merge the non-null option into field_info for processing
                    field_info = {**field_info, **option}
                    # Remove anyOf since we've resolved it
                    field_info.pop("anyOf", None)
                    break

        # Handle $ref (nested object reference)
        if "$ref" in field_info:
            resolved = self._resolve_ref(field_info["$ref"], defs)
            if resolved:
                field_schema["type"] = "object"
                desc = field_info.get("description") or resolved.get("description", "")
                field_schema["description"] = desc
                # Extract model name from $ref (e.g., "#/$defs/Address" -> "Address")
                ref_path = field_info["$ref"]
                if ref_path.startswith("#/$defs/"):
                    model_name = ref_path[8:]  # Remove "#/$defs/" prefix
                    field_schema["model_name"] = model_name
                # Recursively convert nested fields
                nested_props = resolved.get("properties", {})
                nested_required = set(resolved.get("required", []))
                if nested_props:
                    field_schema["nested_model"] = True
                    field_schema["nested_schema"] = {
                        "fields": [
                            self._convert_field(name, info, nested_required, defs)
                            for name, info in nested_props.items()
                        ]
                    }
                return field_schema

        # Get base type
        json_type = field_info.get("type", "string")

        # Map JSON Schema types to UI types
        type_mapping = {
            "string": "string",
            "integer": "number",
            "number": "number",
            "boolean": "boolean",
            "array": "array",
            "object": "object",
        }
        field_schema["type"] = type_mapping.get(json_type, "string")

        # Handle format (date, date-time, email, uri, etc.)
        if "format" in field_info:
            fmt = field_info["format"]
            if fmt == "date":
                field_schema["type"] = "date"
            elif fmt == "date-time":
                field_schema["type"] = "datetime"
            # Could add email, uri, etc. in future

        # Copy description
        field_schema["description"] = field_info.get("description", "")

        # Handle enum (Literal types)
        if "enum" in field_info:
            field_schema["enum"] = field_info["enum"]
            # Enums are always rendered as string dropdowns
            field_schema["type"] = "string"

        # Handle default value
        if "default" in field_info:
            field_schema["default"] = field_info["default"]

        # Handle numeric constraints
        if "minimum" in field_info:
            field_schema["min"] = field_info["minimum"]
        if "maximum" in field_info:
            field_schema["max"] = field_info["maximum"]
        if "exclusiveMinimum" in field_info:
            # exclusiveMinimum means > not >=, add small offset
            val = field_info["exclusiveMinimum"]
            field_schema["min"] = val + (0.01 if isinstance(val, float) else 1)
        if "exclusiveMaximum" in field_info:
            val = field_info["exclusiveMaximum"]
            field_schema["max"] = val - (0.01 if isinstance(val, float) else 1)

        # Handle string constraints
        if "minLength" in field_info:
            field_schema["minLength"] = field_info["minLength"]
        if "maxLength" in field_info:
            field_schema["maxLength"] = field_info["maxLength"]
        if "pattern" in field_info:
            field_schema["pattern"] = field_info["pattern"]

        # Handle examples (for placeholder text)
        if "examples" in field_info and field_info["examples"]:
            field_schema["examples"] = field_info["examples"]

        # Handle x-* extension properties (e.g., x-populate-from, x-show-model-schema)
        for key, value in field_info.items():
            if key.startswith("x-"):
                # Convert to camelCase for JS consumption (x-populate-from -> populateFrom)
                camel_key = "".join(
                    word.capitalize() if i > 0 else word
                    for i, word in enumerate(key[2:].split("-"))
                )
                field_schema[camel_key] = value

        # Handle arrays
        if json_type == "array":
            items_schema = field_info.get("items", {})

            # Empty items (list[Any]) - render as JSON textarea (matches _convert_model_field_to_ui)
            if not items_schema or items_schema == {}:
                field_schema["type"] = "array"
                field_schema["items_type"] = "object"
                return field_schema

            # Check if items is a $ref (list of objects)
            if "$ref" in items_schema:
                resolved = self._resolve_ref(items_schema["$ref"], defs)
                if resolved:
                    field_schema["type"] = "array"
                    field_schema["items_type"] = "object"
                    # Extract model name from $ref (e.g., "#/$defs/EmailAddress" -> "EmailAddress")
                    ref_path = items_schema["$ref"]
                    if ref_path.startswith("#/$defs/"):
                        model_name = ref_path[8:]  # Remove "#/$defs/" prefix
                        field_schema["model_name"] = model_name
                    # Get nested fields for the list item type
                    nested_props = resolved.get("properties", {})
                    nested_required = set(resolved.get("required", []))
                    if nested_props:
                        field_schema["nested_model"] = True
                        field_schema["nested_schema"] = {
                            "fields": [
                                self._convert_field(name, info, nested_required, defs)
                                for name, info in nested_props.items()
                            ]
                        }
            else:
                # Primitive array (list[str], list[int], etc.) or object array without $ref
                item_type = items_schema.get("type", "string")
                item_type_mapping = {
                    "string": "string",
                    "integer": "number",
                    "number": "number",
                    "boolean": "boolean",
                    "object": "object",
                }
                field_schema["items_type"] = item_type_mapping.get(item_type, "string")

                # For object arrays without $ref, check if items has inline properties
                if item_type == "object" and "properties" in items_schema:
                    nested_props = items_schema.get("properties", {})
                    nested_required = set(items_schema.get("required", []))
                    if nested_props:
                        field_schema["nested_model"] = True
                        field_schema["nested_schema"] = {
                            "fields": [
                                self._convert_field(name, info, nested_required, defs)
                                for name, info in nested_props.items()
                            ]
                        }

        return field_schema

    async def scan_server(
        self, server_module: str, server_dir: Path | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Import an MCP server and collect tools with schemas from FastMCP.

        Args:
            server_module: Python module path (e.g., 'mcp_servers.bamboohr.main')
            server_dir: Server directory path (for setting up environment)

        Returns:
            Tuple of (tools, models) where:
            - tools: List of tool metadata dictionaries compatible with UI generator
            - models: List of model definitions extracted from JSON Schema $defs
        """
        # Set GUI_ENABLED environment variable
        old_gui_enabled = os.environ.get("GUI_ENABLED")
        os.environ["GUI_ENABLED"] = "true"

        # Add server directory to sys.path if provided
        if server_dir:
            sys.path.insert(0, str(server_dir))

        try:
            # Remove module from cache to ensure fresh import
            if server_module in sys.modules:
                del sys.modules[server_module]

            # STEP 1: Import the server module to trigger tool registration
            module = importlib.import_module(server_module)

            # Find the FastMCP instance
            mcp_instance = None
            for name, obj in inspect.getmembers(module):
                if type(obj).__name__ == "FastMCP":
                    mcp_instance = obj
                    break

            if not mcp_instance:
                raise RuntimeError(f"No FastMCP instance found in {server_module}")

            # STEP 1.5: Call main() to run full initialization (including setup_auth)
            # MCP_UI_GEN=true tells run_server() to skip mcp.run() and return early
            if hasattr(module, "main") and callable(module.main):
                try:
                    module.main()
                except SystemExit:
                    # main() may call sys.exit() after run_server returns early
                    pass
                except Exception:
                    # main() may fail during UI generation (e.g., missing database)
                    pass

            # STEP 2: Get all registered tools with their schemas from FastMCP
            tools_dict = await mcp_instance.get_tools()
            print(f"\n=== DISCOVERED {len(tools_dict)} REGISTERED TOOLS ===")

            # STEP 3: Convert each tool to UI generator format
            collected_tools = []
            all_referenced_models: set[str] = set()
            all_defs: dict[str, Any] = {}
            for tool_name, tool_obj in tools_dict.items():
                print(f"\n  Processing: {tool_name}")

                # Skip tools without parameters attribute (shouldn't happen but be safe)
                if not hasattr(tool_obj, "parameters"):
                    print("    SKIP: no parameters attribute")
                    continue

                # Skip tools without a function (shouldn't happen but be safe)
                if not hasattr(tool_obj, "fn"):
                    print("    SKIP: no fn attribute")
                    continue

                # Try to get schema from type hints first (preserves nested models)
                # Fall back to FastMCP schema if that fails
                json_schema = self._get_schema_from_type_hints(tool_obj.fn)
                if json_schema:
                    print("    Using schema from type hints (preserves nested models)")
                    # Merge any extra properties from tool_obj.parameters that
                    # aren't in the type-hint schema (e.g. page_number injected
                    # by ResponseLimiterMiddleware.patch_tool_schemas).
                    if hasattr(tool_obj, "parameters") and tool_obj.parameters:
                        extra = tool_obj.parameters.get("properties", {})
                        hint_props = json_schema.get("properties", {})
                        for key, val in extra.items():
                            if key not in hint_props:
                                hint_props[key] = val
                else:
                    # Fall back to FastMCP's schema
                    json_schema = tool_obj.parameters
                    if not json_schema:
                        print("    SKIP: empty schema")
                        continue

                # Extract properties, handling Pydantic model wrapper pattern
                properties, required_fields, defs = self._extract_properties(json_schema)

                # Collect $defs and find referenced models for this tool
                if defs:
                    all_defs.update(defs)
                    referenced = self._find_referenced_models(json_schema, defs)
                    all_referenced_models.update(referenced)

                # Convert each field to UI generator format using _convert_field
                # This handles nested objects, arrays, anyOf, enums, validators, etc.
                fields = []
                for field_name, field_info in properties.items():
                    field_schema = self._convert_field(
                        field_name, field_info, required_fields, defs
                    )
                    fields.append(field_schema)

                parsed_schema = {"fields": fields}

                # Get description from tool object
                description = tool_obj.description if hasattr(tool_obj, "description") else ""

                # Build tool metadata in UI generator format
                tool_info = {
                    "name": tool_name,
                    "function": tool_obj.fn,  # Include function for compatibility
                    "description": description,
                    "parsed_schema": parsed_schema,
                }

                collected_tools.append(tool_info)
                print(f"    SUCCESS: {len(json_schema.get('properties', {}))} parameters")

            print("\n=== COLLECTION SUMMARY ===")
            print(f"Total registered: {len(tools_dict)}")
            print(f"Successfully collected: {len(collected_tools)}")

            # STEP 4: Collect referenced models from $defs
            collected_models = []
            if all_referenced_models and all_defs:
                collected_models = self._collect_referenced_models(all_defs, all_referenced_models)
                print(f"Referenced models: {len(collected_models)}")

            return collected_tools, collected_models

        except Exception as e:
            raise RuntimeError(f"Failed to scan server {server_module}: {e}")

        finally:
            # Restore environment
            if old_gui_enabled is not None:
                os.environ["GUI_ENABLED"] = old_gui_enabled
            elif "GUI_ENABLED" in os.environ:
                del os.environ["GUI_ENABLED"]

            # Remove server_dir from sys.path
            if server_dir and str(server_dir) in sys.path:
                sys.path.remove(str(server_dir))
