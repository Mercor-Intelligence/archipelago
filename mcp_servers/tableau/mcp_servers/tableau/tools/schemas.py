"""Schema management utilities for Tableau MCP server.

This module provides tools to load, list, and inspect JSON schemas
for request and response validation.
"""

import json
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).parent.parent / "schemas" / "v1"


def list_request_schemas() -> list[str]:
    """List all available request JSON schemas."""
    requests_dir = SCHEMA_DIR / "requests"
    if not requests_dir.exists():
        return []
    return [f.stem for f in requests_dir.glob("*.json")]


def list_response_schemas() -> list[str]:
    """List all available response JSON schemas."""
    responses_dir = SCHEMA_DIR / "responses"
    if not responses_dir.exists():
        return []
    return [f.stem for f in responses_dir.glob("*.json")]


def load_json_schema(schema_type: str, name: str) -> dict[str, Any]:
    """Load a JSON schema from schemas/v1/requests or schemas/v1/responses."""
    schema_file = SCHEMA_DIR / schema_type / f"{name}.json"
    if not schema_file.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_file}")
    with open(schema_file) as f:
        return json.load(f)


def schema_tool(
    request_name: str | None = None,
    response_name: str | None = None,
    return_as_string: bool = True,
) -> dict:
    """Tool to inspect Tableau MCP JSON schemas."""
    result = {}

    # No schemas specified, return lists
    if not request_name and not response_name:
        return {
            "available_requests": list_request_schemas(),
            "available_responses": list_response_schemas(),
            "info": "No request or response specified. Listing all available schemas.",
        }

    # Load request schema if requested
    if request_name:
        try:
            request_schema = load_json_schema("requests", request_name)
            result["request_name"] = request_name
            result["request_schema"] = (
                json.dumps(request_schema, indent=4) if return_as_string else request_schema
            )
        except FileNotFoundError:
            result["request_error"] = f"Request schema '{request_name}' not found"

    # Load response schema if requested
    if response_name:
        try:
            response_schema = load_json_schema("responses", response_name)
            result["response_name"] = response_name
            result["response_schema"] = (
                json.dumps(response_schema, indent=4) if return_as_string else response_schema
            )
        except FileNotFoundError:
            result["response_error"] = f"Response schema '{response_name}' not found"

    # Fallback: if response_name not given but request_name is, try matching response
    if request_name and not response_name and "request_error" not in result:
        try:
            response_schema = load_json_schema("responses", request_name)
            result["response_name"] = request_name
            result["response_schema"] = (
                json.dumps(response_schema, indent=4) if return_as_string else response_schema
            )
        except FileNotFoundError:
            # Silently ignore if matching response doesn't exist
            pass

    if "request_error" not in result and "response_error" not in result:
        result["info"] = "Schemas retrieved successfully."
    else:
        result["info"] = "Some schemas could not be loaded (see error fields)."

    return result
