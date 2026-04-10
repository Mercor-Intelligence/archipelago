"""Schema generation and validation tool for Tableau REST API.

This tool fetches live data from Tableau Server REST API and generates
JSON schemas that can be used to validate mock API responses. It compares
generated schemas with existing ones to detect API changes.

Usage:
    python -m mcp_servers.tableau.tools.compare_live_schemas

Requirements:
    - TABLEAU_SERVER_URL in .env
    - TABLEAU_SITE_ID in .env
    - TABLEAU_AUTH_TOKEN in .env (format: token-name:token-secret)
    - TABLEAU_API_VERSION in .env (optional, defaults to 3.27)
"""

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ..logging_config import get_logger
from ..tableau_http.tableau_client import TableauHTTPClient

logger = get_logger(__name__)

load_dotenv()

BASE_URL = os.getenv("TABLEAU_SERVER_URL", "")
SITE_ID = os.getenv("TABLEAU_SITE_ID", "")
AUTH_TOKEN = os.getenv("TABLEAU_AUTH_TOKEN", "")
API_VERSION = os.getenv("TABLEAU_API_VERSION", "3.27")

SCHEMA_DIR = Path(__file__).parent.parent / "schemas" / "v1"


@dataclass
class TableauEndpoint:
    """Configuration for a Tableau REST API endpoint.

    Attributes:
        endpoint_path: API endpoint path template (e.g., "sites/{site_id}/projects")
        entity_name: Singular entity name (e.g., "project")
        is_list: Whether this extracts a list of items vs single item
    """

    endpoint_path: str
    entity_name: str
    is_list: bool = False

    @property
    def type(self) -> str:
        """Return endpoint type (for backward compatibility)."""
        return "list"

    @property
    def endpoint(self) -> str:
        """Return endpoint path (for backward compatibility)."""
        return self.endpoint_path

    @property
    def extract_key(self) -> str:
        """Return the response key to extract data from."""
        return f"{self.entity_name}s"

    @property
    def list_key(self) -> str:
        """Return the key within extract_key containing array of items."""
        return self.entity_name

    @property
    def item_key(self) -> str | None:
        """Return the key for single item extraction."""
        return self.entity_name if not self.is_list else None

    def to_dict(self) -> dict:
        """Convert to dictionary format for backward compatibility."""
        result = {
            "type": self.type,
            "endpoint": self.endpoint,
            "extract_key": self.extract_key,
            "list_key": self.list_key,
        }
        if not self.is_list:
            result["item_key"] = self.item_key
        return result


ENDPOINT_MAPPING = {
    "project": TableauEndpoint("sites/{site_id}/projects", "project"),
    "user": TableauEndpoint("sites/{site_id}/users", "user"),
    "workbook": TableauEndpoint("sites/{site_id}/workbooks", "workbook"),
    "datasource": TableauEndpoint("sites/{site_id}/datasources", "datasource"),
    "group": TableauEndpoint("sites/{site_id}/groups", "group"),
    "project_list": TableauEndpoint("sites/{site_id}/projects", "project", is_list=True),
    "user_list": TableauEndpoint("sites/{site_id}/users", "user", is_list=True),
    "workbook_list": TableauEndpoint("sites/{site_id}/workbooks", "workbook", is_list=True),
    "datasource_list": TableauEndpoint("sites/{site_id}/datasources", "datasource", is_list=True),
    "group_list": TableauEndpoint("sites/{site_id}/groups", "group", is_list=True),
}


def _normalize_endpoint_config(endpoint_config: TableauEndpoint | dict) -> dict:
    """Convert TableauEndpoint to dict if needed."""
    if isinstance(endpoint_config, TableauEndpoint):
        return endpoint_config.to_dict()
    return endpoint_config


async def fetch_live_data(
    client: TableauHTTPClient, endpoint_config: TableauEndpoint | dict
) -> dict:
    """Fetch live data from Tableau REST API."""
    config = _normalize_endpoint_config(endpoint_config)
    endpoint_type = config.get("type")
    endpoint = config["endpoint"].format(site_id=client.site_id)

    if endpoint_type == "list":
        params = {"pageSize": "10", "pageNumber": "1"}
        return await client.get(endpoint, params)
    else:
        raise ValueError(f"Unknown endpoint type: {endpoint_type}")


def _extract_first_item(response_data: dict, extract_key: str, list_key: str) -> dict:
    """Extract first item from a list response.

    Args:
        response_data: Raw API response
        extract_key: Top-level key containing the list container
        list_key: Key within container holding the array

    Returns:
        First item from list, or empty dict if no items
    """
    container = response_data.get(extract_key, {})
    items = container.get(list_key, [])
    return items[0] if items else {}


def _extract_full_list(response_data: dict, extract_key: str, list_key: str) -> list:
    """Extract full list from a list response.

    Args:
        response_data: Raw API response
        extract_key: Top-level key containing the list container
        list_key: Key within container holding the array

    Returns:
        List of items from response
    """
    container = response_data.get(extract_key, {})
    return container.get(list_key, [])


def extract_sample_from_response(
    response_data: dict, endpoint_config: TableauEndpoint | dict
) -> dict | list:
    """Extract a sample object from the API response."""
    config = _normalize_endpoint_config(endpoint_config)
    extract_key = config.get("extract_key")
    item_key = config.get("item_key")
    list_key = config.get("list_key")

    if item_key is not None:
        return _extract_first_item(response_data, extract_key, item_key)

    return _extract_full_list(response_data, extract_key, list_key)


def infer_type(value: Any) -> str:
    """Infer JSON Schema type from a Python value using structural pattern matching."""
    match value:
        case None:
            return "null"
        case bool():
            return "boolean"
        case int():
            return "integer"
        case float():
            return "number"
        case str():
            return "string"
        case list():
            return "array"
        case dict():
            return "object"
        case _:
            return "string"


def _schema_for_array_property(value: list) -> dict:
    """Generate schema for an array property.

    Args:
        value: Array value to analyze

    Returns:
        JSON Schema for the array property
    """
    if not value:
        return {"type": ["array", "null"], "items": {}}

    if isinstance(value[0], dict):
        return {"type": ["array", "null"], "items": generate_schema_from_object(value[0])}

    item_type = infer_type(value[0])
    return {
        "type": ["array", "null"],
        "items": {"type": [item_type, "null"] if item_type != "null" else item_type},
    }


def _schema_for_primitive_property(value: Any) -> dict:
    """Generate schema for a primitive property.

    Args:
        value: Primitive value to analyze

    Returns:
        JSON Schema for the primitive property
    """
    inferred_type = infer_type(value)
    if value is not None and inferred_type != "null":
        schema = {"type": [inferred_type, "null"]}
        schema["example"] = value
    else:
        schema = {"type": inferred_type}
    return schema


def generate_schema_from_object(obj: Any, title: str | None = None) -> dict[str, Any]:
    """Generate JSON Schema from a Python object."""
    if isinstance(obj, dict):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}

        if title:
            schema["title"] = title

        for key, value in obj.items():
            if isinstance(value, dict):
                nested_schema = generate_schema_from_object(value)
                nested_schema["type"] = ["object", "null"]
                schema["properties"][key] = nested_schema
            elif isinstance(value, list):
                schema["properties"][key] = _schema_for_array_property(value)
            else:
                schema["properties"][key] = _schema_for_primitive_property(value)

        return schema

    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            return {"type": "array", "items": generate_schema_from_object(obj[0])}
        return {
            "type": "array",
            "items": {"type": infer_type(obj[0]) if obj else "string"},
        }

    else:
        return {"type": infer_type(obj)}


def _get_entity_name(endpoint_config: TableauEndpoint | dict, schema_name: str) -> str:
    """Extract entity name from endpoint configuration.

    Args:
        endpoint_config: Endpoint configuration
        schema_name: Fallback name if entity name not in config

    Returns:
        Entity name for the schema
    """
    if isinstance(endpoint_config, TableauEndpoint):
        return endpoint_config.extract_key
    return endpoint_config.get("extract_key", schema_name)


def _add_schema_metadata(schema: dict, entity_name: str) -> None:
    """Add metadata fields to a generated schema.

    Args:
        schema: Schema to modify in-place
        entity_name: Name of the entity for description
    """
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    schema["description"] = f"Auto-generated schema from Tableau REST API for {entity_name}"
    schema["generated_at"] = datetime.now(UTC).isoformat()
    schema["generated_from"] = "live_api_data"


async def generate_schema_for_endpoint(
    client: TableauHTTPClient, schema_name: str, endpoint_config: TableauEndpoint | dict
) -> dict[str, Any] | None:
    """Generate schema from live API data for a specific endpoint."""
    try:
        logger.info("Fetching live data for %s...", schema_name)

        live_data = await fetch_live_data(client, endpoint_config)
        live_sample = extract_sample_from_response(live_data, endpoint_config)

        if not live_sample:
            logger.warning("No data returned from API")
            return None

        if isinstance(live_sample, dict):
            logger.info("Got sample data with %s top-level fields", len(live_sample))
        elif isinstance(live_sample, list):
            logger.info("Got sample list with %s items", len(live_sample))

        entity_name = _get_entity_name(endpoint_config, schema_name)
        schema = generate_schema_from_object(
            live_sample, title=f"{entity_name.title()} Response Schema"
        )

        _add_schema_metadata(schema, entity_name)

        return schema

    except Exception as e:
        logger.error("Error: %s", str(e))
        logger.debug("Traceback:", exc_info=True)
        return None


def save_schema(schema: dict, schema_name: str, schema_type: str = "responses"):
    """Save schema to file."""
    output_dir = SCHEMA_DIR / schema_type
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f"{schema_name}.json"

    with open(output_file, "w") as f:
        json.dump(schema, f, indent=2)

    logger.info("Saved to: %s", output_file)


def compare_with_existing(
    new_schema: dict, schema_name: str, schema_type: str = "responses"
) -> dict:
    """Compare new schema with existing one."""
    existing_file = SCHEMA_DIR / schema_type / f"{schema_name}.json"

    if not existing_file.exists():
        return {"exists": False, "changes": None}

    with open(existing_file) as f:
        old_schema = json.load(f)

    old_props = set(old_schema.get("properties", {}).keys())
    new_props = set(new_schema.get("properties", {}).keys())

    added = new_props - old_props
    removed = old_props - new_props

    return {
        "exists": True,
        "added_fields": list(added),
        "removed_fields": list(removed),
        "field_count_old": len(old_props),
        "field_count_new": len(new_props),
    }


def _print_comparison_report(comparison: dict) -> None:
    """Print comparison report between old and new schemas.

    Args:
        comparison: Comparison result dictionary
    """
    logger.info("Comparison with existing schema:")
    logger.info("  Old fields: %s", comparison["field_count_old"])
    logger.info("  New fields: %s", comparison["field_count_new"])

    if comparison["added_fields"]:
        logger.info("  Added: %s", ", ".join(comparison["added_fields"]))
    if comparison["removed_fields"]:
        logger.info("  Removed: %s", ", ".join(comparison["removed_fields"]))

    if not comparison["added_fields"] and not comparison["removed_fields"]:
        logger.info("  No changes detected")


def _print_summary_report(results: list[dict]) -> None:
    """Print summary of schema generation results.

    Args:
        results: List of result dictionaries from schema generation
    """
    logger.info("=" * 80)
    logger.info("GENERATION SUMMARY")
    logger.info("=" * 80)

    successful = sum(1 for r in results if r["success"])
    failed = len(results) - successful

    logger.info("Total Schemas: %s", len(results))
    logger.info("Successfully Generated: %s", successful)
    if failed > 0:
        logger.error("Failed: %s", failed)
    else:
        logger.info("Failed: %s", failed)

    new_schemas = sum(
        1 for r in results if r["success"] and not r.get("comparison", {}).get("exists", False)
    )
    updated_schemas = sum(
        1 for r in results if r["success"] and r.get("comparison", {}).get("exists", False)
    )

    logger.info("New Schemas: %s", new_schemas)
    logger.info("Updated Schemas: %s", updated_schemas)


def _save_generation_report(results: list[dict]) -> Path:
    """Save detailed generation report to JSON file.

    Args:
        results: List of result dictionaries from schema generation

    Returns:
        Path to the saved report file
    """
    report_file = Path(__file__).parent / "schema_generation_report.json"
    with open(report_file, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "server_url": BASE_URL,
                "site_id": SITE_ID,
                "total": len(results),
                "successful": sum(1 for r in results if r["success"]),
                "failed": sum(1 for r in results if not r["success"]),
                "results": results,
            },
            f,
            indent=2,
        )
    return report_file


async def main():
    """Generate schemas from live API data."""
    logger.info("Tableau Schema Generator (Live API -> Schema)")
    logger.info("Server: %s", BASE_URL)
    logger.info("Site ID: %s", SITE_ID)
    logger.info("Using Auth Token: %s...", AUTH_TOKEN[:20] if AUTH_TOKEN else "NOT SET")
    logger.info("Configured endpoints: %s", len(ENDPOINT_MAPPING))

    if not BASE_URL or not SITE_ID or not AUTH_TOKEN:
        logger.error("Missing required environment variables:")
        logger.error("  - TABLEAU_SERVER_URL")
        logger.error("  - TABLEAU_SITE_ID")
        logger.error("  - TABLEAU_AUTH_TOKEN")
        return

    client = TableauHTTPClient(
        base_url=BASE_URL,
        site_id=SITE_ID,
        api_version=API_VERSION,
        personal_access_token=AUTH_TOKEN,
    )

    logger.info("Signing in to Tableau Server...")
    try:
        await client.sign_in()
        logger.info("Signed in successfully (Site ID: %s)", client.site_id)
    except Exception as e:
        logger.error("Sign-in failed: %s", e)
        return

    results = []

    for schema_name, endpoint_config in ENDPOINT_MAPPING.items():
        logger.info("=" * 80)
        logger.info("Processing: %s", schema_name)
        logger.info("=" * 80)

        schema = await generate_schema_for_endpoint(client, schema_name, endpoint_config)

        if not schema:
            results.append({"schema": schema_name, "success": False})
            continue

        comparison = compare_with_existing(schema, schema_name, "responses")

        if comparison["exists"]:
            _print_comparison_report(comparison)
        else:
            logger.info("New schema (no existing file)")

        save_schema(schema, schema_name, "responses")

        results.append({"schema": schema_name, "success": True, "comparison": comparison})

    _print_summary_report(results)

    report_file = _save_generation_report(results)
    logger.info("Detailed report saved to: %s", report_file)


if __name__ == "__main__":
    asyncio.run(main())
