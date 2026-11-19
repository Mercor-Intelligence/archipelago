"""Command-line interface for mcp-testing framework.

Provides automated acceptance testing workflow with minimal configuration.

Usage:
    mcp-test generate --config config.yaml
    mcp-test generate --api tableau --endpoints /workbooks,/dashboards
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

from mcp_testing import FixtureGenerator, HTTPClient


def load_config(config_path: str) -> dict[str, Any]:
    """Load configuration from YAML file."""
    if not YAML_AVAILABLE:
        print("ERROR: PyYAML not installed. Install with: pip install pyyaml")
        sys.exit(1)

    config_file = Path(config_path)
    if not config_file.exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_file) as f:
        config = yaml.safe_load(f)

    # Validate config is not None (empty YAML file or only comments)
    if config is None:
        print(f"ERROR: Config file is empty or contains only comments: {config_path}")
        print("Please add configuration content to the file.")
        sys.exit(1)

    # Validate config is a dictionary
    if not isinstance(config, dict):
        print(f"ERROR: Config file must contain a YAML dictionary, got {type(config).__name__}")
        print("Expected format:")
        print("api:")
        print("  base_url: https://api.example.com")
        sys.exit(1)

    return config


def get_auth_token(config: dict[str, Any]) -> str | None:
    """Get authentication token from environment."""
    auth_config = config.get("api", {}).get("auth", {})
    token_env_var = auth_config.get("token_env_var")

    if not token_env_var:
        return None

    token = os.getenv(token_env_var)
    if not token:
        print(f"WARNING: {token_env_var} environment variable not set")
        print(f"Set it with: export {token_env_var}='your-token-here'")

    return token


async def auto_generate_test_cases(
    endpoint_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Auto-generate comprehensive test cases for an endpoint.

    Generates:
    - Success cases (empty, non-empty, filtered)
    - Error cases (400, 401, 403, 404, 422)
    """
    endpoint_path = endpoint_config["path"]
    endpoint_name = endpoint_config["name"]
    methods = endpoint_config.get("methods", ["GET"])

    test_cases = []

    # Get user-defined test cases
    user_test_cases = endpoint_config.get("test_cases", {})

    # Add success cases
    test_cases.extend(
        {
            "name": f"{endpoint_name} - {success_case['name']}",
            "endpoint": endpoint_path.format(**success_case.get("path_params", {})),
            "method": success_case.get("method", methods[0]),
            "params": success_case.get("params", {}),
            "note": f"Success case: {success_case['name']}",
        }
        for success_case in user_test_cases.get("success", [])
    )

    # Add error cases
    test_cases.extend(
        {
            "name": (
                f"{endpoint_name} - {error_case['name']} "
                f"({error_case.get('expected_status', '4xx')})"
            ),
            "endpoint": endpoint_path.format(**error_case.get("path_params", {})),
            "method": error_case.get("method", methods[0]),
            "params": error_case.get("params", {}),
            "override_auth": error_case.get("override_auth"),
            "note": (
                f"Error case ({error_case.get('expected_status', '4xx')}): {error_case['name']}"
            ),
        }
        for error_case in user_test_cases.get("errors", [])
    )

    return test_cases


async def generate_fixtures(config: dict[str, Any]) -> None:
    """Generate fixtures from live API based on config.

    This implements the complete acceptance testing workflow:
    1. Connect to live API
    2. Collect success and error cases for all endpoints
    3. Save as fixtures
    4. Generate pytest tests
    """
    print("=" * 70)
    print("MCP Testing - Automated Fixture Generation")
    print("=" * 70)

    # Extract config
    api_config = config.get("api", {})
    endpoints_config = config.get("endpoints", [])
    output_config = config.get("output", {})
    options = config.get("options", {})

    api_name = api_config.get("name", "API")
    base_url = api_config.get("base_url")
    fixtures_dir = output_config.get("fixtures_dir", "fixtures")
    tool_name = output_config.get("tool_name", "api_tool")
    tests_dir = output_config.get("tests_dir", "tests")

    # Validate required config
    if not base_url:
        raise ValueError(
            "Missing required configuration: 'api.base_url' must be specified in the YAML config"
        )

    print(f"\nAPI: {api_name}")
    print(f"Base URL: {base_url}")
    print(f"Endpoints: {len(endpoints_config)}")
    print(f"Output: {fixtures_dir}/\n")

    # Get auth token
    auth_token = get_auth_token(config)
    if not auth_token:
        print("\nWARNING: No auth token available")
        print("Some endpoints may fail with 401 errors")
        print("This is expected and will be captured as error fixtures\n")

    # Setup generator
    generator = FixtureGenerator(
        http_client=HTTPClient(base_url=base_url, auth_token=auth_token),
        output_dir=fixtures_dir,
    )

    # Collect all test cases
    all_test_cases = []
    for endpoint_config in endpoints_config:
        test_cases = await auto_generate_test_cases(endpoint_config)
        all_test_cases.extend(test_cases)

    print(f"Generated {len(all_test_cases)} test cases")
    print("\nCapturing fixtures from live API...")
    print("-" * 70)

    # Capture fixtures
    success_count = 0
    error_count = 0

    for i, test_case in enumerate(all_test_cases, 1):
        print(f"\n[{i}/{len(all_test_cases)}] {test_case['name']}")

        try:
            # Determine subdirectory by checking for parenthesized status codes
            # (same approach as auto_testing.py to avoid false positives)
            error_status_patterns = ["(400)", "(401)", "(403)", "(404)", "(422)", "(4xx)", "(5xx)"]
            is_error = any(pattern in test_case["name"] for pattern in error_status_patterns)
            subdirectory = "errors" if is_error else None

            path = await generator.capture_response(
                name=test_case["name"],
                endpoint=test_case["endpoint"],
                method=test_case.get("method", "GET"),
                params=test_case.get("params"),
                override_auth=test_case.get("override_auth"),
                subdirectory=subdirectory,
            )

            if subdirectory == "errors":
                error_count += 1
                print(f"  [PASS] Error fixture captured: {path.name}")
            else:
                success_count += 1
                print(f"  [PASS] Success fixture captured: {path.name}")

        except Exception as e:
            print(f"  [WARN] Could not capture: {e}")

    # Auto-capture common errors if enabled
    if options.get("auto_capture_errors", True):
        print("\n" + "-" * 70)
        print("Auto-capturing common error scenarios...")

        for endpoint_config in endpoints_config:
            endpoint = endpoint_config["path"]
            methods = endpoint_config.get("methods", ["GET"])

            try:
                error_paths = await generator.capture_error_scenarios(
                    endpoint=endpoint, method=methods[0], subdirectory="errors"
                )
                error_count += len(error_paths)
                print(f"  [PASS] Captured {len(error_paths)} errors for {endpoint}")
            except Exception as e:
                print(f"  [WARN] Could not auto-capture errors for {endpoint}: {e}")

    # Summary
    print("\n" + "=" * 70)
    print("FIXTURE COLLECTION SUMMARY")
    print("=" * 70)
    print(f"Success fixtures: {success_count}")
    print(f"Error fixtures: {error_count}")
    print(f"Total fixtures: {success_count + error_count}")

    if success_count + error_count == 0:
        print("\n[FAIL] No fixtures captured!", file=sys.stderr)
        print("Check that:", file=sys.stderr)
        print(f"  1. API is accessible at {base_url}", file=sys.stderr)
        print("  2. Auth token is valid", file=sys.stderr)
        print("  3. Endpoints are correct", file=sys.stderr)
        return

    # Generate pytest tests
    if options.get("auto_generate_tests", True):
        print("\n" + "-" * 70)
        print("Generating pytest tests...")

        test_file = Path(tests_dir) / f"test_{tool_name}_acceptance.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            generator.generate_test_file(
                fixtures="**/*.json", output_file=str(test_file), tool_name=tool_name
            )
            print(f"[PASS] Generated: {test_file}")
        except Exception as e:
            print(f"[FAIL] Could not generate tests: {e}", file=sys.stderr)

    # Final instructions
    print("\n" + "=" * 70)
    print("COMPLETE!")
    print("=" * 70)
    print(f"\n[PASS] Fixtures saved to: {fixtures_dir}/")
    print(f"[PASS] Tests generated: {tests_dir}/test_{tool_name}_acceptance.py")
    print("\n[INFO] Next Steps:")
    print(f"  1. Review fixtures in {fixtures_dir}/")
    print(f"  2. Implement your MCP tool: {tool_name}")
    print(f"  3. Run: pytest {tests_dir}/test_{tool_name}_acceptance.py -v")
    print("  4. Fix failures until all tests pass")
    print("  5. Your MCP server is ready!")


def create_sample_config(api_name: str, endpoints: list[str]) -> None:
    """Create a sample configuration file."""
    config_content = f"""# MCP Testing Configuration for {api_name}
# Generated by mcp-test

api:
  name: "{api_name}"
  base_url: "https://api.{api_name.lower()}.com/v1"  # TODO: Update with real URL
  auth:
    type: "bearer"
    token_env_var: "{api_name.upper()}_API_TOKEN"

endpoints:
"""

    for endpoint in endpoints:
        endpoint_name = endpoint.strip("/").replace("/", "_")
        config_content += f"""  - name: "{endpoint_name}"
    path: "{endpoint}"
    methods: ["GET"]
    test_cases:
      success:
        - name: "list_all"
          params: {{}}
      errors:
        - name: "unauthorized"
          override_auth: "INVALID_TOKEN"
          expected_status: 401

"""

    config_content += """output:
  fixtures_dir: "fixtures"
  tests_dir: "tests"
  tool_name: "api_tool"

options:
  auto_capture_errors: true
  auto_generate_tests: true
"""

    config_file = Path("config.yaml")
    with open(config_file, "w") as f:
        f.write(config_content)

    print(f"[PASS] Created config file: {config_file}")
    print("\nNext steps:")
    print("  1. Edit config.yaml with your API details")
    print(f"  2. Set environment variable: export {api_name.upper()}_API_TOKEN='your-token'")
    print("  3. Run: mcp-test generate --config config.yaml")


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="MCP Testing Framework - Automated acceptance testing"
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Generate command
    generate_parser = subparsers.add_parser(
        "generate", help="Generate fixtures and tests from config"
    )
    generate_parser.add_argument("--config", "-c", required=True, help="Path to config YAML file")

    # Init command
    init_parser = subparsers.add_parser("init", help="Create sample config file")
    init_parser.add_argument("--api", "-a", required=True, help="API name (e.g., Tableau)")
    init_parser.add_argument(
        "--endpoints",
        "-e",
        required=True,
        help="Comma-separated endpoints (e.g., /workbooks,/dashboards)",
    )

    args = parser.parse_args()

    if args.command == "generate":
        config = load_config(args.config)
        asyncio.run(generate_fixtures(config))

    elif args.command == "init":
        endpoints = [e.strip() for e in args.endpoints.split(",")]
        create_sample_config(args.api, endpoints)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
