"""
Schema Converter - Convert Pydantic schemas to TypeScript config format.
"""

import re
from typing import Any


class SchemaConverter:
    """Convert Pydantic schemas to TypeScript DataType config format."""

    # Type mapping from Python to TypeScript/UI types
    TYPE_MAPPING = {
        "str": "string",
        "int": "number",
        "float": "number",
        "bool": "boolean",
        "date": "date",
        "datetime": "datetime",
        "time": "string",
        "None": "null",
    }

    def __init__(self):
        pass

    def convert_to_typescript(
        self,
        tool_info: dict[str, Any],
        parsed_schema: dict[str, Any],
        build_spec: dict[str, Any],
        server_name: str,
    ) -> dict[str, Any]:
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

        if override:
            if "display_name" in override:
                display_name = override["display_name"]
            if "category" in override:
                category = override["category"]

        # Build the TypeScript config
        config = {
            "id": f"{server_name}-{self._to_kebab_case(tool_info['name'])}",
            "name": display_name,
            "category": category,
            "description": tool_info.get("description", ""),
            "server": server_name,
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
                "type": self._map_type(field["type"]),
                "required": field["required"],
                "description": field["description"],
                "location": "body",  # Default to body for POST requests
            }

            # Add validators
            validators = field.get("validators", {})

            if "min_length" in validators:
                param["minLength"] = validators["min_length"]
            if "max_length" in validators:
                param["maxLength"] = validators["max_length"]
            if "gt" in validators:
                # Greater than -> min should be slightly higher
                gt_val = validators["gt"]
                param["min"] = gt_val + 0.01 if isinstance(gt_val, float) else gt_val + 1
            if "ge" in validators:
                param["min"] = validators["ge"]
            if "lt" in validators:
                lt_val = validators["lt"]
                param["max"] = lt_val - 0.01 if isinstance(lt_val, float) else lt_val - 1
            if "le" in validators:
                param["max"] = validators["le"]
            if "pattern" in validators:
                param["pattern"] = validators["pattern"]

            # Add placeholder from examples
            if "examples" in field and field["examples"]:
                example = field["examples"][0]
                param["placeholder"] = f"e.g., {example}"

            # Add default value
            if "default" in field and field["default"] is not None:
                # Convert default to JSON-serializable format
                default_val = field["default"]
                # Skip Pydantic undefined/special types
                if not str(type(default_val).__name__).startswith("Pydantic"):
                    param["default"] = default_val

            # Handle enums (Literal types)
            if "Literal" in field["type"]:
                # Extract literal values
                enum_values = self._extract_literal_values(field["type"])
                if enum_values:
                    param["enum"] = enum_values
                    param["type"] = "string"  # Enums are rendered as dropdowns

            # Handle nested models (simplified as JSON for now)
            if "nested_model" in field:
                param["type"] = "object"
                param["isJsonField"] = True
                param["jsonExample"] = self._generate_example_json(field.get("nested_schema", {}))

            parameters.append(param)

        return parameters

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
        """Get tool override from build spec."""
        overrides = build_spec.get("tool_overrides", [])
        for override in overrides:
            if override.get("tool") == tool_path:
                return override
        return None

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

    def convert_sample_data_to_typescript(
        self, sample_data_tables: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Convert sample data tables to TypeScript format.

        Args:
            sample_data_tables: List of sample data table dictionaries

        Returns:
            List of TypeScript-formatted sample data tables
        """
        result = []

        for table in sample_data_tables:
            ts_table = {
                "tableName": table["table_name"],
                "modelName": table["model_name"],
                "description": table["description"],
                "rowCount": table["row_count"],
                "csvContent": table["csv_content"],
                "server": table["server"],
            }
            result.append(ts_table)

        return result
