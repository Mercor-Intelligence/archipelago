"""Schema validation tool for Tableau MCP server.

This tool validates that response data matches the generated JSON schemas.
It tests both happy paths (valid data) and sad paths (invalid data should
be rejected).

Usage:
    python -m mcp_servers.tableau.tools.validate_schemas

Requirements:
    - Generated schemas in schemas/v1/responses/
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from ..logging_config import get_logger
from .schemas import list_response_schemas, load_json_schema

logger = get_logger(__name__)


def _get_primary_type(schema_type: str | list | None) -> str | None:
    """Extract primary type from schema type (handles nullable arrays).

    Args:
        schema_type: Type value from schema (string or array like ["string", "null"])

    Returns:
        Primary type as string, or None if not found
    """
    if isinstance(schema_type, list):
        return schema_type[0] if schema_type else None
    return schema_type


def build_example_from_schema(schema: dict[str, Any]) -> dict[str, Any] | list | Any:
    """Build an example data object from a schema with examples."""
    if "example" in schema:
        return schema["example"]

    primary_type = _get_primary_type(schema.get("type"))

    if primary_type == "object":
        properties = schema.get("properties", {})
        example = {}
        for key, prop_schema in properties.items():
            example[key] = build_example_from_schema(prop_schema)
        return example

    if primary_type == "array":
        items_schema = schema.get("items", {})
        return [build_example_from_schema(items_schema)]

    match primary_type:
        case "string":
            return schema.get("example", "example_string")
        case "integer":
            return schema.get("example", 0)
        case "number":
            return schema.get("example", 0.0)
        case "boolean":
            return schema.get("example", True)
        case "null":
            return None
        case _:
            return None


def generate_invalid_examples(
    schema: dict[str, Any], valid_example: dict[str, Any]
) -> list[dict[str, Any]]:
    """Generate invalid data examples that should fail validation."""
    invalid_cases = []
    primary_schema_type = _get_primary_type(schema.get("type"))
    properties = schema.get("properties", {})

    if primary_schema_type != "object" or not properties:
        return invalid_cases

    for prop_name, prop_schema in properties.items():
        prop_type = _get_primary_type(prop_schema.get("type"))
        valid_value = valid_example.get(prop_name)

        match prop_type:
            case "string" if isinstance(valid_value, str):
                invalid_data = valid_example.copy()
                invalid_data[prop_name] = 12345
                invalid_cases.append(
                    {
                        "description": f"Wrong type for {prop_name}: integer instead of string",
                        "data": invalid_data,
                    }
                )

            case "integer" if isinstance(valid_value, int):
                invalid_data = valid_example.copy()
                invalid_data[prop_name] = "not an integer"
                invalid_cases.append(
                    {
                        "description": f"Wrong type for {prop_name}: string instead of integer",
                        "data": invalid_data,
                    }
                )

            case "boolean" if isinstance(valid_value, bool):
                invalid_data = valid_example.copy()
                invalid_data[prop_name] = "true"
                invalid_cases.append(
                    {
                        "description": f"Wrong type for {prop_name}: string instead of boolean",
                        "data": invalid_data,
                    }
                )

            case "object" if isinstance(valid_value, dict):
                invalid_data = valid_example.copy()
                invalid_data[prop_name] = "not an object"
                invalid_cases.append(
                    {
                        "description": f"Wrong type for {prop_name}: string instead of object",
                        "data": invalid_data,
                    }
                )

            case "array" if isinstance(valid_value, list):
                invalid_data = valid_example.copy()
                invalid_data[prop_name] = "not an array"
                invalid_cases.append(
                    {
                        "description": f"Wrong type for {prop_name}: string instead of array",
                        "data": invalid_data,
                    }
                )

    required_fields = schema.get("required", [])
    if required_fields:
        for req_field in required_fields[:1]:
            if req_field in valid_example:
                invalid_data = {k: v for k, v in valid_example.items() if k != req_field}
                invalid_cases.append(
                    {
                        "description": f"Missing required field: {req_field}",
                        "data": invalid_data,
                    }
                )

    extra_property_data = valid_example.copy()
    extra_property_data["__invalid_extra_field__"] = "should not exist"
    invalid_cases.append(
        {
            "description": "Extra property not in schema",
            "data": extra_property_data,
            "should_pass": True,
        }
    )

    for prop_name, prop_schema in properties.items():
        prop_primary_type = _get_primary_type(prop_schema.get("type"))
        if prop_primary_type == "object" and isinstance(valid_example.get(prop_name), dict):
            nested_obj = valid_example.get(prop_name, {})
            if nested_obj:
                nested_props = prop_schema.get("properties", {})
                for nested_key, nested_schema in list(nested_props.items())[:1]:
                    nested_type = _get_primary_type(nested_schema.get("type"))
                    if nested_type == "string":
                        invalid_nested = nested_obj.copy()
                        invalid_nested[nested_key] = 999
                        invalid_data = valid_example.copy()
                        invalid_data[prop_name] = invalid_nested
                        invalid_cases.append(
                            {
                                "description": f"Wrong type in nested {prop_name}.{nested_key}:"
                                + "integer instead of string",
                                "data": invalid_data,
                            }
                        )
                        break

    string_props = [
        (name, schema)
        for name, schema in properties.items()
        if _get_primary_type(schema.get("type")) == "string"
        and isinstance(valid_example.get(name), str)
    ]
    if string_props:
        prop_name, _ = string_props[0]
        invalid_data = valid_example.copy()
        invalid_data[prop_name] = None
        invalid_cases.append(
            {
                "description": f"Null value for string field: {prop_name}",
                "data": invalid_data,
                "should_pass": True,
            }
        )

    return invalid_cases


def validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate data against a JSON schema."""
    validator = Draft7Validator(schema)
    errors = []

    for error in validator.iter_errors(data):
        path = ".".join(str(p) for p in error.path) if error.path else "root"
        errors.append(f"{path}: {error.message}")

    return errors


def _print_validation_result(
    schema_name: str, test_type: str, description: str, errors: list[str], expected_valid: bool
) -> dict:
    """Print validation results for a test case.

    Args:
        schema_name: Name of the schema being validated
        test_type: Type of test (happy_path or sad_path)
        description: Description of the test case
        errors: List of validation errors
        expected_valid: Whether validation was expected to pass

    Returns:
        Result dictionary for reporting
    """
    is_valid = len(errors) == 0
    test_passed = is_valid == expected_valid

    if test_passed:
        status = "[PASS]"
    else:
        status = "[FAIL]"

    if test_type == "happy_path":
        logger.info("  %s Happy path: %s", status, description)
    else:
        logger.info("  %s Sad path: %s", status, description)

    if not test_passed and errors:
        for error in errors[:3]:
            logger.error("       Error: %s", error)

    return {
        "schema": schema_name,
        "test_type": test_type,
        "description": description,
        "expected_valid": expected_valid,
        "actual_valid": is_valid,
        "passed": test_passed,
        "errors": errors if not test_passed else [],
    }


async def validate_schemas() -> list[dict]:
    """Validate schemas with both happy and sad path tests."""
    logger.info("Validating schemas with happy and sad path tests...")

    results = []
    available_schemas = list_response_schemas()

    for schema_name in available_schemas:
        logger.info("=" * 80)
        logger.info("Testing: %s", schema_name)
        logger.info("=" * 80)

        try:
            schema = load_json_schema("responses", schema_name)
            valid_example = build_example_from_schema(schema)

            if valid_example is None:
                logger.info("  [SKIP] Could not build example data")
                continue

            errors = validate_against_schema(valid_example, schema)
            result = _print_validation_result(
                schema_name,
                "happy_path",
                "Valid data should pass validation",
                errors,
                expected_valid=True,
            )
            results.append(result)

            if not isinstance(valid_example, dict):
                logger.info("  [SKIP] Skipping sad path tests (not an object schema)")
                continue

            invalid_cases = generate_invalid_examples(schema, valid_example)

            for case in invalid_cases:
                should_pass = case.get("should_pass", False)
                errors = validate_against_schema(case["data"], schema)
                result = _print_validation_result(
                    schema_name,
                    "sad_path",
                    case["description"],
                    errors,
                    expected_valid=should_pass,
                )
                results.append(result)

        except FileNotFoundError:
            logger.error("  [ERROR] Schema file not found: %s", schema_name)
            results.append(
                {
                    "schema": schema_name,
                    "test_type": "error",
                    "description": "Schema file not found",
                    "passed": False,
                    "errors": ["File not found"],
                }
            )
        except Exception as e:
            logger.error("  [ERROR] %s", str(e))
            results.append(
                {
                    "schema": schema_name,
                    "test_type": "error",
                    "description": str(e),
                    "passed": False,
                    "errors": [str(e)],
                }
            )

    return results


def _print_summary_report(results: list[dict]) -> None:
    """Print summary of validation results.

    Args:
        results: List of validation result dictionaries
    """
    logger.info("=" * 80)
    logger.info("VALIDATION SUMMARY")
    logger.info("=" * 80)

    total = len(results)
    passed = sum(1 for r in results if r.get("passed", False))
    failed = total - passed

    happy_path_tests = [r for r in results if r.get("test_type") == "happy_path"]
    sad_path_tests = [r for r in results if r.get("test_type") == "sad_path"]

    happy_passed = sum(1 for r in happy_path_tests if r.get("passed", False))
    sad_passed = sum(1 for r in sad_path_tests if r.get("passed", False))

    logger.info("Total Tests: %s", total)
    logger.info("Passed: %s", passed)
    logger.info("Failed: %s", failed)
    logger.info("Happy Path Tests: %s (%s passed)", len(happy_path_tests), happy_passed)
    logger.info("Sad Path Tests: %s (%s passed)", len(sad_path_tests), sad_passed)

    if failed > 0:
        logger.error("Failed tests:")
        for result in results:
            if not result.get("passed", False):
                schema = result.get("schema", "unknown")
                desc = result.get("description", "unknown")
                logger.error("   - %s: %s", schema, desc)


def _save_validation_report(results: list[dict]) -> Path:
    """Save detailed validation report to JSON file.

    Args:
        results: List of validation result dictionaries

    Returns:
        Path to the saved report file
    """
    from datetime import UTC, datetime

    report_file = Path(__file__).parent / "schema_validation_report.json"

    happy_path_tests = [r for r in results if r.get("test_type") == "happy_path"]
    sad_path_tests = [r for r in results if r.get("test_type") == "sad_path"]

    with open(report_file, "w") as f:
        json.dump(
            {
                "validated_at": datetime.now(UTC).isoformat(),
                "total_tests": len(results),
                "passed": sum(1 for r in results if r.get("passed", False)),
                "failed": sum(1 for r in results if not r.get("passed", False)),
                "happy_path_tests": len(happy_path_tests),
                "happy_path_passed": sum(1 for r in happy_path_tests if r.get("passed", False)),
                "sad_path_tests": len(sad_path_tests),
                "sad_path_passed": sum(1 for r in sad_path_tests if r.get("passed", False)),
                "results": results,
            },
            f,
            indent=2,
        )
    return report_file


async def main():
    """Run schema validation."""
    logger.info("Tableau Schema Validator (Happy + Sad Paths)")

    results = await validate_schemas()

    _print_summary_report(results)

    report_file = _save_validation_report(results)
    logger.info("Detailed report saved to: %s", report_file)


if __name__ == "__main__":
    asyncio.run(main())
