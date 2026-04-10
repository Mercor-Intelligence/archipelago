#!/usr/bin/env python3
"""
Generate OpenAPI documentation from Pydantic models.

Usage:
    uv run python scripts/generate_openapi.py <server_name>

Example:
    uv run python scripts/generate_openapi.py weather
"""

import argparse
import importlib.util
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from logging_config import get_logger

logger = get_logger(__name__)


def load_models_module(server_name: str, base_path: Path):
    """Dynamically load the models module for a server."""
    models_path = base_path / "mcp_servers" / server_name / "models.py"

    if not models_path.exists():
        logger.error("models.py not found for server '%s'", server_name)
        logger.error("Expected: %s", models_path)
        logger.error("Tip: Server must be created with --with-models flag")
        sys.exit(1)

    # Load the module
    spec = importlib.util.spec_from_file_location("models", models_path)
    models = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(models)

    return models


def pydantic_to_openapi_schema(model_class):
    """Convert a Pydantic model to OpenAPI schema."""
    schema = model_class.model_json_schema()
    schema.pop("$defs", None)  # Remove internal refs
    return schema


def generate_openapi_spec(server_name: str, models_module) -> dict:
    """Generate OpenAPI specification from Pydantic models."""
    # Find Input and Output models
    input_model = None
    output_model = None

    for name in dir(models_module):
        obj = getattr(models_module, name)
        if hasattr(obj, "model_json_schema"):  # It's a Pydantic model
            if name.endswith("Input"):
                input_model = obj
            elif name.endswith("Output"):
                output_model = obj

    if not input_model or not output_model:
        logger.error("Could not find Input/Output models in models.py")
        logger.error("Expected models ending with 'Input' and 'Output'")
        sys.exit(1)

    # Build OpenAPI spec
    spec = {
        "openapi": "3.0.0",
        "info": {
            "title": f"{server_name.replace('_', ' ').title()} API",
            "version": "1.0.0",
            "description": f"MCP Server API for {server_name}",
        },
        "paths": {
            f"/{server_name}": {
                "post": {
                    "summary": f"Execute {server_name} tool",
                    "description": input_model.__doc__ or f"Process {server_name} request",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": pydantic_to_openapi_schema(input_model)}
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Successful response",
                            "content": {
                                "application/json": {
                                    "schema": pydantic_to_openapi_schema(output_model)
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    return spec


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate OpenAPI documentation from Pydantic models",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/generate_openapi.py weather
  python scripts/generate_openapi.py email_validator
        """,
    )
    parser.add_argument(
        "server_name",
        help="Name of the MCP server (must have models.py)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file path (default: docs/<server_name>_openapi.yaml)",
    )

    args = parser.parse_args()

    # Get base path
    script_path = Path(__file__).resolve()
    base_path = script_path.parent.parent

    # Load models
    models_module = load_models_module(args.server_name, base_path)

    # Generate OpenAPI spec
    spec = generate_openapi_spec(args.server_name, models_module)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        docs_dir = base_path / "docs"
        docs_dir.mkdir(exist_ok=True)
        output_path = docs_dir / f"{args.server_name}_openapi.yaml"

    # Write YAML
    with open(output_path, "w") as f:
        yaml.dump(spec, f, default_flow_style=False, sort_keys=False)

    logger.info("Generated OpenAPI spec: %s", output_path)
    logger.info("View online: https://editor.swagger.io/")
    logger.info("Or paste the contents of %s into the editor", output_path)


if __name__ == "__main__":
    main()
