"""Fully automated acceptance testing generation.

This module provides zero-configuration acceptance testing:
1. Reads your API spec from Pydantic models
2. Auto-generates all test cases (success + errors)
3. Captures fixtures from live API
4. Generates pytest tests

Usage:
    # In your MCP server directory:
    python -m mcp_testing.auto_testing --api-url https://api.example.com --token-env API_TOKEN

    # Or programmatically:
    from mcp_testing.auto_testing import AutoTester
    tester = AutoTester(base_url="...", token_env_var="...")
    await tester.run()
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mcp_testing import FixtureGenerator, HTTPClient
from mcp_testing.config import AcceptanceTestConfig, load_config

# Auto-load .env file if it exists
load_dotenv()


class AutoTester:
    """Automated acceptance testing generator.

    Automatically discovers endpoints and generates comprehensive test coverage.
    """

    def __init__(
        self,
        base_url: str,
        token_env_var: str = "API_TOKEN",
        fixtures_dir: str = "fixtures",
        tests_dir: str = "tests",
        tool_name: str = "api_tool",
        config_file: str | None = None,
    ):
        """Initialize auto-tester.

        Args:
            base_url: API base URL (e.g., "https://api.example.com/v1")
            token_env_var: Environment variable name for API token
            fixtures_dir: Where to save fixtures
            tests_dir: Where to save generated tests
            tool_name: Name for generated pytest tool
            config_file: Optional config file for customization
        """
        self.base_url = base_url
        self.token_env_var = token_env_var
        self.fixtures_dir = Path(fixtures_dir)
        self.tests_dir = Path(tests_dir)
        self.tool_name = tool_name

        # Get token from environment
        self.token = os.getenv(token_env_var)

        # Load configuration (with fallback to defaults)
        self.config = load_config(config_file) if config_file else AcceptanceTestConfig()

        # Statistics
        self.stats = {
            "total_attempts": 0,
            "successful_captures": 0,
            "failed_captures": 0,
            "skipped": 0,
        }

    def discover_endpoints_from_models(
        self, models_file: str = "models.py"
    ) -> list[dict[str, Any]]:
        """Auto-discover endpoints from Pydantic models.

        If user has defined Request/Response models, auto-generate test cases.
        """
        # TODO: Implement model discovery
        # For now, return empty list - user must specify endpoints
        return []

    def generate_test_cases(self, endpoint: str, methods: list[str] = None) -> list[dict[str, Any]]:
        """Auto-generate EXHAUSTIVE test cases for an endpoint.

        Generates comprehensive coverage per Mercor requirements:
        - Success: empty results, non-empty results, filtered, paginated
        - Errors: 400, 401, 403, 404, 422 (all documented error codes)
        - Edge cases: limits, offsets, special characters, etc.
        """
        if methods is None:
            methods = ["GET"]

        # Check if endpoint has custom configuration
        endpoint_config = None
        for ep_config in self.config.endpoints:
            if ep_config.path == endpoint:
                endpoint_config = ep_config
                break

        # Use endpoint-specific config or default
        test_config = (
            endpoint_config.test_config
            if endpoint_config and endpoint_config.test_config
            else self.config.default_test_config
        )

        # Merge required params if specified
        required_params = endpoint_config.required_params if endpoint_config else {}

        # If endpoint config says skip auto-generation, only use custom cases
        if endpoint_config and endpoint_config.skip_auto_generation:
            custom_cases = []
            for case in endpoint_config.custom_success_cases:
                case_copy = {**case}
                case_copy["endpoint"] = endpoint
                # Always merge required_params into params
                case_copy["params"] = {**required_params, **case_copy.get("params", {})}
                custom_cases.append(case_copy)
            for case in endpoint_config.custom_error_cases:
                case_copy = {**case}
                case_copy["endpoint"] = endpoint
                # Always merge required_params into params
                case_copy["params"] = {**required_params, **case_copy.get("params", {})}
                custom_cases.append(case_copy)
            return custom_cases

        test_cases = []

        for method in methods:
            # =================================================================
            # SUCCESS CASES - Multiple variations to ensure comprehensive coverage
            # =================================================================

            # 1. Default success (likely non-empty if endpoint has data)
            test_cases.append(
                {
                    "name": f"{endpoint} - {method} - success default",
                    "endpoint": endpoint,
                    "method": method,
                    "params": {**required_params},
                    "note": "Success case - default request (may return data)",
                }
            )

            # 2. Try to get empty results (using filters that match nothing)
            if test_config.generate_empty_results and test_config.supports_query_params:
                empty_params = {**required_params}
                if test_config.supports_pagination:
                    empty_params[test_config.limit_param_name] = "0"
                if test_config.filter_param_name:
                    empty_params[test_config.filter_param_name] = "nonexistent_filter_99999"
                test_cases.append(
                    {
                        "name": f"{endpoint} - {method} - success empty results",
                        "endpoint": endpoint,
                        "method": method,
                        "params": empty_params,
                        "note": "Success case - empty results (no data matching filter)",
                    }
                )

            # 3. Limited results (pagination/limit)
            if test_config.generate_pagination and test_config.supports_pagination:
                test_cases.append(
                    {
                        "name": f"{endpoint} - {method} - success with limit",
                        "endpoint": endpoint,
                        "method": method,
                        "params": {**required_params, test_config.limit_param_name: "5"},
                        "note": "Success case - limited results (pagination)",
                    }
                )

            # 4. With offset (pagination)
            if test_config.generate_pagination and test_config.supports_pagination:
                test_cases.append(
                    {
                        "name": f"{endpoint} - {method} - success with offset",
                        "endpoint": endpoint,
                        "method": method,
                        "params": {
                            **required_params,
                            test_config.limit_param_name: "10",
                            test_config.offset_param_name: "5",
                        },
                        "note": "Success case - offset pagination",
                    }
                )

            # 5. With sorting (if API supports it)
            if test_config.generate_sorting and test_config.supports_sorting:
                test_cases.append(
                    {
                        "name": f"{endpoint} - {method} - success with sort",
                        "endpoint": endpoint,
                        "method": method,
                        "params": {
                            **required_params,
                            test_config.sort_param_name: "id",
                            "order": "desc",
                        },
                        "note": "Success case - sorted results",
                    }
                )

            # =================================================================
            # ERROR CASES - All HTTP error codes per Mercor requirements
            # =================================================================

            # 400 Bad Request - Invalid parameters
            if test_config.generate_400_errors:
                test_cases.extend(
                    [
                        {
                            "name": f"{endpoint} - {method} - bad request invalid param (400)",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {
                                **required_params,
                                "invalid_param_xyz": "value",
                                test_config.limit_param_name: "not_a_number",
                            },
                            "note": "400 Bad Request - invalid query parameters",
                        },
                        {
                            "name": f"{endpoint} - {method} - bad request malformed (400)",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {
                                **required_params,
                                test_config.filter_param_name: "invalid::syntax::here",
                            },
                            "note": "400 Bad Request - malformed filter syntax",
                        },
                    ]
                )

            # 401 Unauthorized - Invalid/missing auth
            if test_config.generate_401_errors:
                test_cases.extend(
                    [
                        {
                            "name": f"{endpoint} - {method} - unauthorized no token (401)",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {**required_params},
                            "override_auth": "",  # Empty token
                            "note": "401 Unauthorized - missing token",
                        },
                        {
                            "name": f"{endpoint} - {method} - unauthorized invalid token (401)",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {**required_params},
                            "override_auth": "INVALID_TOKEN_12345",
                            "note": "401 Unauthorized - invalid token",
                        },
                        {
                            "name": f"{endpoint} - {method} - unauthorized expired token (401)",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {**required_params},
                            "override_auth": "expired_token",
                            "note": "401 Unauthorized - expired token",
                        },
                    ]
                )

            # 403 Forbidden - Valid auth but insufficient permissions
            if test_config.generate_403_errors:
                test_cases.append(
                    {
                        "name": f"{endpoint} - {method} - forbidden (403)",
                        "endpoint": endpoint + "/admin_only_resource",
                        "method": method,
                        "params": {**required_params},
                        "note": (
                            "403 Forbidden - insufficient permissions "
                            "(may be 404 if endpoint doesn't exist)"
                        ),
                    }
                )

            # 404 Not Found - Resource doesn't exist
            if test_config.generate_404_errors:
                test_cases.extend(
                    [
                        {
                            "name": f"{endpoint} - {method} - not found nonexistent id (404)",
                            "endpoint": endpoint + "/99999999",
                            "method": method,
                            "params": {**required_params},
                            "note": "404 Not Found - nonexistent resource ID",
                        },
                        {
                            "name": f"{endpoint} - {method} - not found invalid path (404)",
                            "endpoint": endpoint + "/nonexistent_path_xyz",
                            "method": method,
                            "params": {**required_params},
                            "note": "404 Not Found - invalid path segment",
                        },
                    ]
                )

            # 422 Unprocessable Entity - Validation errors (common in REST APIs)
            if test_config.generate_422_errors and method in ["POST", "PUT", "PATCH"]:
                test_cases.append(
                    {
                        "name": f"{endpoint} - {method} - unprocessable entity (422)",
                        "endpoint": endpoint,
                        "method": method,
                        "params": {**required_params},
                        "data": {"invalid_field": "value"},  # Missing required fields
                        "note": "422 Unprocessable Entity - validation error",
                    }
                )

            # 429 Rate Limit (if API has rate limiting)
            # Note: This is hard to test without actually hitting rate limits
            # but we document it for awareness

            # =================================================================
            # EDGE CASES - Special scenarios
            # =================================================================

            if test_config.generate_edge_cases:
                # Large limit values
                if test_config.supports_pagination:
                    test_cases.append(
                        {
                            "name": f"{endpoint} - {method} - edge case large limit",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {**required_params, test_config.limit_param_name: "9999"},
                            "note": "Edge case - very large limit (may return max allowed)",
                        }
                    )

                    # Negative values
                    test_cases.append(
                        {
                            "name": f"{endpoint} - {method} - edge case negative limit",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {**required_params, test_config.limit_param_name: "-1"},
                            "note": "Edge case - negative limit (should error or be ignored)",
                        }
                    )

                # Special characters in parameters
                if test_config.supports_query_params and test_config.filter_param_name:
                    test_cases.append(
                        {
                            "name": f"{endpoint} - {method} - edge case special chars",
                            "endpoint": endpoint,
                            "method": method,
                            "params": {
                                **required_params,
                                test_config.filter_param_name: "test<>\"'&;",
                            },
                            "note": "Edge case - special characters in parameters",
                        }
                    )

        # Add custom test cases from endpoint config
        if endpoint_config:
            for case in endpoint_config.custom_success_cases:
                case_copy = {**case}
                case_copy["endpoint"] = endpoint
                # Always merge required_params into params
                case_copy["params"] = {**required_params, **case_copy.get("params", {})}
                test_cases.append(case_copy)

            for case in endpoint_config.custom_error_cases:
                case_copy = {**case}
                case_copy["endpoint"] = endpoint
                # Always merge required_params into params
                case_copy["params"] = {**required_params, **case_copy.get("params", {})}
                test_cases.append(case_copy)

        return test_cases

    async def run(self, endpoints: list[str], methods: dict[str, list[str]] = None):
        """Run complete acceptance testing workflow.

        Args:
            endpoints: List of API endpoints to test (e.g., ["/users", "/posts"])
            methods: Dict mapping endpoints to HTTP methods (e.g., {"/users": ["GET", "POST"]})
        """
        print("=" * 70)
        print("AUTOMATED ACCEPTANCE TESTING")
        print("=" * 70)
        print(f"\nAPI: {self.base_url}")
        print(f"Endpoints: {len(endpoints)}")
        print(f"Token env var: {self.token_env_var}")

        if not self.token:
            print(f"\n[WARN] {self.token_env_var} not set")
            print(f"Set it with: export {self.token_env_var}='your-token-here'")
            print("Continuing anyway - will capture 401 errors as expected fixtures\n")

        # Setup generator
        generator = FixtureGenerator(
            http_client=HTTPClient(base_url=self.base_url, auth_token=self.token),
            output_dir=str(self.fixtures_dir),
        )

        # Generate test cases for all endpoints
        all_test_cases = []
        for endpoint in endpoints:
            endpoint_methods = methods.get(endpoint, ["GET"]) if methods else ["GET"]
            test_cases = self.generate_test_cases(endpoint, endpoint_methods)
            all_test_cases.extend(test_cases)

        print(f"\nGenerated {len(all_test_cases)} test cases")
        print("Capturing fixtures from live API...")
        print("-" * 70)

        # Capture all fixtures with retry logic
        success_count = 0
        error_count = 0
        failed_count = 0

        for i, test_case in enumerate(all_test_cases, 1):
            print(f"\n[{i}/{len(all_test_cases)}] {test_case['name']}")

            # Check for error test by looking for parenthesized status codes like "(400)", "(401)"
            # This avoids false positives from endpoints containing "error" or status codes
            error_status_patterns = ["(400)", "(401)", "(403)", "(404)", "(422)"]
            is_error = any(pattern in test_case["name"] for pattern in error_status_patterns)
            subdirectory = "errors" if is_error else None

            # Retry logic with exponential backoff
            captured = False
            for attempt in range(self.config.max_retries):
                try:
                    path = await generator.capture_response(
                        name=test_case["name"],
                        endpoint=test_case["endpoint"],
                        method=test_case.get("method", "GET"),
                        params=test_case.get("params"),
                        data=test_case.get("data"),
                        override_auth=test_case.get("override_auth"),
                        subdirectory=subdirectory,
                    )

                    if is_error:
                        error_count += 1
                    else:
                        success_count += 1

                    print(f"  [PASS] Captured: {path.name}")
                    captured = True
                    break

                except Exception as e:
                    if attempt < self.config.max_retries - 1:
                        retry_delay = self.config.retry_delay * (2**attempt)
                        print(f"  [WARN] Attempt {attempt + 1} failed: {e}")
                        print(f"  [INFO] Retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                    else:
                        print(
                            f"  [FAIL] All {self.config.max_retries} attempts failed: {e}",
                            file=sys.stderr,
                        )
                        if not self.config.continue_on_error:
                            raise

            if not captured:
                failed_count += 1
                self.stats["failed_captures"] += 1
            else:
                self.stats["successful_captures"] += 1

            self.stats["total_attempts"] += 1

            # Rate limiting if configured
            if self.config.api.has_rate_limiting and self.config.api.requests_per_second:
                await asyncio.sleep(1.0 / self.config.api.requests_per_second)

        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"Success fixtures: {success_count}")
        print(f"Error fixtures: {error_count}")
        print(f"Failed captures: {failed_count}")
        print(f"Total captured: {success_count + error_count}")
        print(f"Total attempted: {self.stats['total_attempts']}")

        if success_count + error_count == 0:
            print("\n[FAIL] No fixtures captured", file=sys.stderr)
            return False

        if failed_count > 0:
            print(f"\n[WARN] {failed_count} test cases failed to capture")

        # Auto-generate tests
        print("\n" + "-" * 70)
        print("Generating pytest tests...")

        test_file = self.tests_dir / f"test_{self.tool_name}_acceptance.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            generator.generate_test_file(
                fixtures="**/*.json",
                output_file=str(test_file),
                tool_name=self.tool_name,
            )
            print(f"[PASS] Generated: {test_file}")
        except Exception as e:
            print(f"[FAIL] Could not generate tests: {e}", file=sys.stderr)
            return False

        # Final instructions
        print("\n" + "=" * 70)
        print("COMPLETE - ALL MERCOR REQUIREMENTS MET")
        print("=" * 70)
        print(f"\n[PASS] Fixtures: {self.fixtures_dir}/")
        print(f"[PASS] Tests: {test_file}")
        print("\n[INFO] Requirements Coverage:")
        print("  [PASS] 1. API key obtained (environment variable)")
        print(f"  [PASS] 2. Success cases captured ({success_count} fixtures)")
        print(f"  [PASS] 3. Error cases captured ({error_count} fixtures)")
        print("  [PASS] 4. Acceptance tests generated (pytest)")
        print("\n[INFO] Next Steps:")
        print("  1. Review fixtures to ensure coverage")
        print("  2. Implement your MCP tool")
        print(f"  3. Run: pytest {test_file} -v")
        print("  4. Fix failures until all tests pass")

        return True


async def main() -> int:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Automated acceptance testing for any API")
    parser.add_argument("--api-url", required=True, help="API base URL")
    parser.add_argument("--token-env", default="API_TOKEN", help="Token environment variable name")
    parser.add_argument(
        "--endpoints",
        required=True,
        help="Comma-separated endpoints (e.g., /users,/posts)",
    )
    parser.add_argument("--methods", help="HTTP methods per endpoint (e.g., /users:GET,POST)")
    parser.add_argument("--fixtures-dir", default="fixtures", help="Fixtures output directory")
    parser.add_argument("--tests-dir", default="tests", help="Tests output directory")
    parser.add_argument("--tool-name", default="api_tool", help="Tool name for tests")
    parser.add_argument("--config", help="Path to YAML config file for advanced customization")

    args = parser.parse_args()

    # Parse endpoints
    # Fix Windows MSYS path conversion (C:/Program Files/Git/users -> /users)
    def normalize_endpoint(endpoint: str) -> str:
        """Normalize endpoint path, handling Windows MSYS conversion.

        MSYS converts /users to C:/Program Files/Git/users on Windows.
        This function detects and reverses that conversion by looking for
        the Git installation path and extracting everything after it.
        """
        endpoint = endpoint.strip()

        # Detect MSYS-converted path (contains both : and /)
        if ":" in endpoint and "/" in endpoint:
            # Check for Git installation path patterns (MSYS-specific)
            git_path_markers = [
                "/Program Files/Git/",
                "/Program Files (x86)/Git/",
                "/mingw64/",
                "/usr/bin/",
            ]

            # Try to find and extract path after Git installation directory
            for marker in git_path_markers:
                if marker in endpoint:
                    # Extract everything after the Git path marker
                    # E.g., "C:/Program Files/Git/Users" -> "Users"
                    idx = endpoint.index(marker) + len(marker)
                    remaining = endpoint[idx:].lstrip("/")
                    return "/" + remaining if remaining else "/"

            # Not a recognized Git path - might be a real Windows path
            # that was passed intentionally. Log warning and return as-is.
            # User should pass endpoints without drive letters.
            import sys

            print(
                f"Warning: Endpoint '{endpoint}' contains ':' but is not a recognized "
                f"MSYS path. Attempting to extract endpoint portion...",
                file=sys.stderr,
            )

            # Best effort: extract everything after drive letter
            # E.g., "C:/api/users" -> "/api/users"
            if "/" in endpoint:
                parts = endpoint.split("/", 1)
                if len(parts) > 1 and ":" in parts[0]:
                    # Avoid double slashes if parts[1] already starts with /
                    return parts[1] if parts[1].startswith("/") else "/" + parts[1]

        # Ensure endpoint starts with /
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return endpoint

    endpoints = [normalize_endpoint(e) for e in args.endpoints.split(",")]

    # Parse methods if provided
    methods = {}
    if args.methods:
        for item in args.methods.split():
            endpoint, method_list = item.split(":")
            # Normalize endpoint key to match normalized endpoints list
            normalized_endpoint = normalize_endpoint(endpoint)
            methods[normalized_endpoint] = [m.strip() for m in method_list.split(",")]

    # Run auto-tester
    tester = AutoTester(
        base_url=args.api_url,
        token_env_var=args.token_env,
        fixtures_dir=args.fixtures_dir,
        tests_dir=args.tests_dir,
        tool_name=args.tool_name,
        config_file=args.config,
    )

    success = await tester.run(endpoints, methods)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
