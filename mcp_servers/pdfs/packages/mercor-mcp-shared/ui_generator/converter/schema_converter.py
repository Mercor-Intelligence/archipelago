"""
Schema Converter - Convert Pydantic schemas to TypeScript config format.
"""

import fnmatch
import re
from typing import Any


class SchemaConverter:
    """Convert Pydantic schemas to TypeScript DataType config format."""

    # Type mapping from Python to TypeScript/UI types
    TYPE_MAPPING = {
        "str": "string",
        "string": "string",
        "int": "number",
        "integer": "number",
        "float": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "date": "date",
        "datetime": "datetime",
        "time": "string",
        "None": "null",
        "object": "object",
        "array": "array",
    }

    def __init__(self):
        pass

    def convert_to_typescript(
        self,
        tool_info: dict[str, Any],
        parsed_schema: dict[str, Any],
        build_spec: dict[str, Any],
        server_name: str,
    ) -> dict[str, Any] | None:
        """
        Convert Pydantic schema to TypeScript DataType format.

        Args:
            tool_info: Tool metadata (name, description, etc.)
            parsed_schema: Parsed Pydantic schema from PydanticParser
            build_spec: Build specification dict
            server_name: Name of the server this tool belongs to

        Returns:
            TypeScript config dictionary
        """
        # Get server config
        server_config = self._get_server_config(server_name, build_spec)

        # Get category for this tool
        category = self._get_category(tool_info["name"], server_name, build_spec)

        # Get display name
        display_name = self._format_display_name(tool_info["name"])

        # Check for overrides
        override = self._get_tool_override(f"{server_name}.{tool_info['name']}", build_spec)

        is_hidden = False
        if override:
            # Check if tool should be hidden from sidebar (but still in dataTypes)
            is_hidden = override.get("hidden", False)
            if "display_name" in override:
                display_name = override["display_name"]
            if "category" in override:
                category = override["category"]

        # Build the TypeScript config
        config = {
            "id": f"{server_name}-{self._to_kebab_case(tool_info['name'])}",
            "name": display_name,
            "toolName": tool_info["name"],  # Original tool name for reliable identification
            "category": category,
            "description": tool_info.get("description", ""),
            "server": server_name,
            "hidden": is_hidden,  # Hidden tools are in dataTypes but filtered from sidebar
            "_internal": {
                "method": tool_info.get("method", "POST"),
                # Relative URL - will use dynamic API base from getApiBase()
                "url": f"/tools/{tool_info['name']}",
                "requiresAuth": server_config.get("requires_auth", True),
            },
        }

        # Add icon if available
        if override and "icon" in override:
            config["icon"] = override["icon"]
        elif "icon" in server_config:
            config["icon"] = server_config["icon"]

        # Convert parameters
        if parsed_schema and "fields" in parsed_schema:
            parameters = self._convert_parameters(parsed_schema["fields"])
            if parameters:
                config["parameters"] = parameters

        return config

    def _convert_parameters(self, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Pydantic fields to UI parameter format."""
        parameters = []

        for field in fields:
            param = {
                "name": field["name"],
                "label": self._format_label(field["name"]),
                "type": self._map_type(field.get("type", "string")),
                "required": field.get("required", False),
                "description": field.get("description", ""),
                "location": "body",  # Default to body for POST requests
            }

            # Handle validators - support both old format (nested in validators dict)
            # and new format (flat at field level from runtime scanner)
            validators = field.get("validators", {})

            # minLength/maxLength - check both locations
            if "min_length" in validators:
                param["minLength"] = validators["min_length"]
            elif "minLength" in field:
                param["minLength"] = field["minLength"]

            if "max_length" in validators:
                param["maxLength"] = validators["max_length"]
            elif "maxLength" in field:
                param["maxLength"] = field["maxLength"]

            # min/max - check both locations
            if "gt" in validators:
                gt_val = validators["gt"]
                param["min"] = gt_val + 0.01 if isinstance(gt_val, float) else gt_val + 1
            elif "ge" in validators:
                param["min"] = validators["ge"]
            elif "min" in field:
                param["min"] = field["min"]

            if "lt" in validators:
                lt_val = validators["lt"]
                param["max"] = lt_val - 0.01 if isinstance(lt_val, float) else lt_val - 1
            elif "le" in validators:
                param["max"] = validators["le"]
            elif "max" in field:
                param["max"] = field["max"]

            # pattern - check both locations
            if "pattern" in validators:
                param["pattern"] = validators["pattern"]
            elif "pattern" in field:
                param["pattern"] = field["pattern"]

            # Add placeholder from examples
            if "examples" in field and field["examples"]:
                examples = field["examples"]
                if examples:
                    example = examples[0]
                    # Format example nicely for placeholder
                    if isinstance(example, list | dict):
                        import json

                        param["placeholder"] = f"e.g., {json.dumps(example)}"
                    else:
                        param["placeholder"] = f"e.g., {example}"

            # Add default value
            if "default" in field and field["default"] is not None:
                default_val = field["default"]
                # Skip Pydantic undefined/special types
                if not str(type(default_val).__name__).startswith("Pydantic"):
                    param["default"] = default_val

            # Handle enums (Literal types)
            if "enum" in field and field["enum"]:
                param["enum"] = field["enum"]
                param["type"] = "string"  # Enums are rendered as dropdowns
            elif isinstance(field.get("type"), str) and "Literal" in field["type"]:
                # Fallback: extract literal values from type string
                enum_values = self._extract_literal_values(field["type"])
                if enum_values:
                    param["enum"] = enum_values
                    param["type"] = "string"

            # Handle enumDescriptions if provided (for rich dropdown labels)
            if "enum_descriptions" in field and field["enum_descriptions"]:
                param["enumDescriptions"] = field["enum_descriptions"]

            # Handle x-* extension properties (populateFrom, etc.)
            # These come from json_schema_extra in Pydantic fields
            populate_keys = (
                "populateFrom",
                "populateField",
                "populateValue",
                "populateDisplay",
                "populateDependencies",
            )
            for key in populate_keys:
                if key in field:
                    param[key] = field[key]

            # Handle list types FIRST: set isList flag and use item type as the main type
            # This must come before nested model handling to properly detect list[Model] types
            if param["type"] == "array" or field.get("type") == "array":
                # New format: items_type is provided directly by runtime scanner
                if "items_type" in field:
                    item_type = self._map_type(field["items_type"])
                else:
                    # Old format: extract from type string
                    item_type = self._extract_array_item_type(field.get("type", ""))

                if item_type:
                    param["type"] = item_type  # Use the item type as the main type
                    param["isList"] = True
                    # Check if we have a model name (for list[ModelName] types)
                    if "model_name" in field:
                        # Store the model reference separately
                        param["modelRef"] = field["model_name"]
                    elif item_type == "object" and "nested_schema" in field:
                        # For list of nested models without a name, embed the fields
                        nested_fields = field["nested_schema"].get("fields", [])
                        if nested_fields:
                            # Warn: embedding fields instead of using modelRef
                            import sys

                            field_name = param.get("name", "unknown")
                            print(
                                f"WARNING: Parameter '{field_name}' is embedding "
                                f"{len(nested_fields)} fields instead of using modelRef. "
                                "This may indicate the scanner failed to extract "
                                "the model name from a $ref.",
                                file=sys.stderr,
                            )
                            param["fields"] = self._convert_fields_to_ui(nested_fields)
                        else:
                            # Fallback to JSON if no field info
                            param["isJsonField"] = True
                    elif item_type == "object":
                        param["isJsonField"] = True
                else:
                    # Fallback: unknown item type, keep as array with string items
                    param["type"] = "string"
                    param["isList"] = True

            # Handle single nested models (not lists) - extract sub-fields for structured input
            elif "nested_model" in field:
                param["type"] = "object"
                # Check if we have a model name (for ModelName types)
                if "model_name" in field:
                    # Store the model reference separately
                    param["modelRef"] = field["model_name"]

                # Only embed fields if no model reference available
                if "model_name" not in field:
                    nested_schema = field.get("nested_schema", {})
                    nested_fields = nested_schema.get("fields", [])

                    if nested_fields:
                        # Warn: embedding fields instead of using modelRef
                        import sys

                        field_name = param.get("name", "unknown")
                        print(
                            f"WARNING: Parameter '{field_name}' is embedding "
                            f"{len(nested_fields)} fields instead of using modelRef. "
                            "This may indicate the scanner failed to extract "
                            "the model name from a $ref.",
                            file=sys.stderr,
                        )
                        # Recursively convert sub-fields to UI format
                        param["fields"] = self._convert_fields_to_ui(nested_fields)
                    else:
                        # Fallback to JSON input if no field info available
                        param["isJsonField"] = True
                        param["jsonExample"] = self._generate_example_json(nested_schema)

            parameters.append(param)

        return parameters

    def _convert_fields_to_ui(self, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Convert nested Pydantic fields to UI field format.

        This is similar to _convert_parameters but designed for nested object fields.
        It recursively handles deeply nested objects.
        """
        ui_fields = []

        for field in fields:
            ui_field: dict[str, Any] = {
                "name": field["name"],
                "label": self._format_label(field["name"]),
                "type": self._map_type(field.get("type", "string")),
                "required": field.get("required", False),
                "description": field.get("description", ""),
            }

            # Handle validators - support both old format (nested in validators dict)
            # and new format (flat at field level from runtime scanner)
            validators = field.get("validators", {})

            # minLength/maxLength
            if "min_length" in validators:
                ui_field["minLength"] = validators["min_length"]
            elif "minLength" in field:
                ui_field["minLength"] = field["minLength"]

            if "max_length" in validators:
                ui_field["maxLength"] = validators["max_length"]
            elif "maxLength" in field:
                ui_field["maxLength"] = field["maxLength"]

            # min/max - check both locations (gt/lt take priority over ge/le)
            if "gt" in validators:
                gt_val = validators["gt"]
                ui_field["min"] = gt_val + 0.01 if isinstance(gt_val, float) else gt_val + 1
            elif "ge" in validators:
                ui_field["min"] = validators["ge"]
            elif "min" in field:
                ui_field["min"] = field["min"]

            if "lt" in validators:
                lt_val = validators["lt"]
                ui_field["max"] = lt_val - 0.01 if isinstance(lt_val, float) else lt_val - 1
            elif "le" in validators:
                ui_field["max"] = validators["le"]
            elif "max" in field:
                ui_field["max"] = field["max"]

            # pattern
            if "pattern" in validators:
                ui_field["pattern"] = validators["pattern"]
            elif "pattern" in field:
                ui_field["pattern"] = field["pattern"]

            # Add default value
            if "default" in field and field["default"] is not None:
                default_val = field["default"]
                if not str(type(default_val).__name__).startswith("Pydantic"):
                    ui_field["default"] = default_val

            # Handle enums (Literal types)
            if "enum" in field and field["enum"]:
                ui_field["enum"] = field["enum"]
                ui_field["type"] = "string"
            elif isinstance(field.get("type"), str) and "Literal" in field.get("type", ""):
                enum_values = self._extract_literal_values(field["type"])
                if enum_values:
                    ui_field["enum"] = enum_values
                    ui_field["type"] = "string"

            # Handle nested models recursively
            if "nested_model" in field:
                ui_field["type"] = "object"
                nested_schema = field.get("nested_schema", {})
                nested_fields = nested_schema.get("fields", [])
                if nested_fields:
                    ui_field["fields"] = self._convert_fields_to_ui(nested_fields)

            # Handle list types
            if ui_field["type"] == "array" or field.get("type") == "array":
                # New format: items_type is provided directly
                if "items_type" in field:
                    item_type = self._map_type(field["items_type"])
                else:
                    # Old format: extract from type string
                    item_type = self._extract_array_item_type(field.get("type", ""))

                if item_type:
                    ui_field["type"] = item_type
                    ui_field["isList"] = True
                    # For list of nested models, extract the nested fields
                    if item_type == "object":
                        if "nested_schema" in field:
                            nested_fields = field["nested_schema"].get("fields", [])
                            if nested_fields:
                                ui_field["fields"] = self._convert_fields_to_ui(nested_fields)
                            else:
                                ui_field["isJsonField"] = True
                        else:
                            ui_field["isJsonField"] = True
                else:
                    ui_field["type"] = "string"
                    ui_field["isList"] = True

            ui_fields.append(ui_field)

        return ui_fields

    def _map_type(self, python_type: str) -> str:
        """Map Python type to UI type."""
        # Handle Optional types
        if python_type.startswith("Optional["):
            inner_type = python_type[9:-1]  # Extract inner type
            return self._map_type(inner_type)

        # Handle List types
        if python_type.startswith("List["):
            return "array"

        # Handle Dict types
        if python_type.startswith("Dict["):
            return "object"

        # Handle Union types (simplified to first type)
        if python_type.startswith("Union["):
            types = python_type[6:-1].split(", ")
            return self._map_type(types[0])

        # Direct mapping
        for py_type, ts_type in self.TYPE_MAPPING.items():
            if py_type in python_type:
                return ts_type

        # Default to string
        return "string"

    def _format_label(self, field_name: str) -> str:
        """Convert snake_case field name to Title Case label."""
        words = field_name.split("_")
        return " ".join(word.capitalize() for word in words)

    def _format_display_name(self, tool_name: str) -> str:
        """Convert snake_case tool name to Title Case display name."""
        return self._format_label(tool_name)

    def _to_kebab_case(self, name: str) -> str:
        """Convert name to kebab-case."""
        return name.replace("_", "-").lower()

    def _get_server_config(self, server_name: str, build_spec: dict[str, Any]) -> dict[str, Any]:
        """Get server configuration from build spec."""
        servers = build_spec.get("servers", [])
        for server in servers:
            if server["name"] == server_name:
                return server

        raise ValueError(f"Server '{server_name}' not found in build spec")

    def _get_category(self, tool_name: str, server_name: str, build_spec: dict[str, Any]) -> str:
        """Determine category for a tool."""
        # Check tool overrides first
        override = self._get_tool_override(f"{server_name}.{tool_name}", build_spec)
        if override and "category" in override:
            return override["category"]

        # Check if server has a default category
        categories = build_spec.get("categories", [])
        for category in categories:
            if server_name in category.get("servers", []):
                return category["name"]

        # Default to server display name
        server_config = self._get_server_config(server_name, build_spec)
        return server_config.get("display_name", server_name.title())

    def _get_tool_override(
        self, tool_path: str, build_spec: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Get tool override from build spec.

        Supports glob patterns in tool names (e.g., "*import_csv" matches "greenhouse.import_csv").
        Exact matches take precedence over glob matches.
        """
        overrides = build_spec.get("tool_overrides", [])

        # First pass: look for exact match
        for override in overrides:
            if override.get("tool") == tool_path:
                return override

        # Second pass: look for glob pattern match
        for override in overrides:
            pattern = override.get("tool", "")
            if "*" in pattern or "?" in pattern:
                if fnmatch.fnmatch(tool_path, pattern):
                    return override

        return None

    def _extract_array_item_type(self, python_type: str) -> str | None:
        """
        Extract the item type from a List/list type annotation.

        Args:
            python_type: Python type string like "List[str]", "list[int]", "List[UserModel]",
                         or "Optional[List[UserModel]]"

        Returns:
            Mapped UI type for the item, or None if not determinable
        """
        # Unwrap Optional types first
        if python_type.startswith("Optional[") and python_type.endswith("]"):
            python_type = python_type[9:-1]  # Remove "Optional[" and trailing "]"

        # Check for List[...] or list[...] patterns
        prefixes = ["List[", "list["]
        inner_type = None

        for prefix in prefixes:
            if python_type.startswith(prefix) and python_type.endswith("]"):
                # Extract inner type, handling nested generics
                bracket_count = 0
                start_idx = len(prefix)
                for i, char in enumerate(python_type):
                    if char == "[":
                        bracket_count += 1
                    elif char == "]":
                        bracket_count -= 1
                        if bracket_count == 0:
                            inner_type = python_type[start_idx:i]
                            break
                break

        if not inner_type:
            return None

        # Map the inner type to a UI type
        mapped_type = self._map_type(inner_type)

        # If the mapped type is "string" but the inner type looks like a class name
        # (starts with uppercase), treat it as an object type
        if mapped_type == "string" and inner_type and inner_type[0].isupper():
            return "object"

        return mapped_type

    def _extract_literal_values(self, type_str: str) -> list[str]:
        """Extract values from Literal type string."""
        # Example: "Literal['draft', 'sent', 'paid']" -> ['draft', 'sent', 'paid']
        match = re.search(r"Literal\[(.*?)\]", type_str)
        if match:
            values_str = match.group(1)
            # Remove quotes and split
            values = [v.strip().strip("'\"") for v in values_str.split(",")]
            return values
        return []

    def _generate_example_json(self, schema: dict[str, Any]) -> str:
        """Generate example JSON string for nested objects."""
        if not schema or "fields" not in schema:
            return "{}"

        example = {}
        for field in schema["fields"]:
            field_name = field["name"]
            field_type = field["type"]

            if "examples" in field and field["examples"]:
                example[field_name] = field["examples"][0]
            elif "default" in field:
                example[field_name] = field["default"]
            elif field_type == "str":
                example[field_name] = "string"
            elif field_type == "int" or field_type == "float":
                example[field_name] = 0
            elif field_type == "bool":
                example[field_name] = True
            else:
                example[field_name] = None

        import json

        return json.dumps(example, indent=2)
