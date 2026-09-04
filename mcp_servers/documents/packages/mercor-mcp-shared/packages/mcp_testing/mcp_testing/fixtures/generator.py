"""Fixture generation from live API responses."""

import re
from pathlib import Path
from typing import Any

import httpx

from ..live_api.client import HTTPClient
from .loader import FixtureLoader
from .models import FixtureExpected, FixtureModel, FixtureRequest


class FixtureGenerator:
    """
    Generate test fixtures from live API responses.

    This tool captures real API responses and saves them as fixtures for
    acceptance testing. Perfect for building an exhaustive spread of test cases.

    Example:
        ```python
        from mcp_testing import FixtureGenerator, HTTPClient

        # Setup generator
        generator = FixtureGenerator(
            http_client=HTTPClient(
                base_url="https://api.taxjar.com",
                auth_token="your_api_key"
            ),
            output_dir="fixtures/"
        )

        # Capture success case
        await generator.capture_response(
            name="List refunds - empty",
            endpoint="/v2/transactions/refunds",
            method="GET",
            params={"from_transaction_date": "2015/05/01"}
        )

        # Capture error case
        await generator.capture_response(
            name="List refunds - unauthorized",
            endpoint="/v2/transactions/refunds",
            method="GET",
            params={},
            override_auth="INVALID_TOKEN"
        )
        ```
    """

    def __init__(
        self,
        http_client: HTTPClient,
        output_dir: str | Path,
        auto_name: bool = True,
    ):
        """
        Initialize fixture generator.

        Args:
            http_client: HTTP client configured for the live API
            output_dir: Directory to save generated fixtures
            auto_name: Auto-generate filenames from test names
        """
        self.http_client = http_client
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.auto_name = auto_name
        self.loader = FixtureLoader(self.output_dir)

    async def capture_response(
        self,
        name: str,
        endpoint: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        override_auth: str | None = None,
        override_headers: dict[str, str] | None = None,
        note: str | None = None,
        filename: str | None = None,
        subdirectory: str | None = None,
    ) -> Path:
        """
        Capture a live API response and save as a fixture.

        Args:
            name: Test name/description
            endpoint: API endpoint path
            method: HTTP method
            params: Query parameters
            data: Request body
            override_auth: Override auth token (for testing errors)
            override_headers: Override headers (for testing errors)
            note: Additional notes about this test case
            filename: Custom filename (auto-generated if None)
            subdirectory: Subdirectory within output_dir

        Returns:
            Path to saved fixture file

        Example:
            ```python
            # Capture success case
            path = await generator.capture_response(
                name="Get user by ID - success",
                endpoint="/users/1",
                method="GET"
            )

            # Capture 404 error
            path = await generator.capture_response(
                name="Get user by ID - not found",
                endpoint="/users/99999",
                method="GET"
            )

            # Capture 401 error with invalid token
            path = await generator.capture_response(
                name="Get user - unauthorized",
                endpoint="/users/1",
                method="GET",
                override_auth="INVALID_TOKEN"
            )
            ```
        """
        # Prepare headers
        headers = None
        if override_auth is not None:
            headers = {"Authorization": f"Bearer {override_auth}"}
        if override_headers:
            headers = {**(headers or {}), **override_headers}

        # Call live API
        try:
            status, response_data = await self.http_client.request(
                method=method,
                endpoint=endpoint,
                data=data,
                params=params,
                headers=headers,
            )
        except Exception as e:
            # Capture exception - could be network error, timeout, etc.
            # Check if this is an HTTP error with a status code
            if isinstance(e, httpx.HTTPStatusError):
                # Server responded with an error status
                status = e.response.status_code
                try:
                    response_data = e.response.json()
                except Exception:
                    response_data = {"error": e.response.text}
            else:
                # Connection error, timeout, DNS failure, etc. - not a server 500
                # Use 0 to indicate no response received
                status = 0
                response_data = {
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "note": "Connection error - no response from server",
                }

        # Detect if this is an error response
        error_contains = None
        if status >= 400 or status == 0:
            # Extract error messages for validation (status=0 means connection error)
            error_contains = self._extract_error_messages(response_data)

        # Create fixture
        fixture = FixtureModel(
            name=name,
            request=FixtureRequest(
                method=method,
                endpoint=endpoint,
                params=params,
                body=data,
                headers=override_headers,
            ),
            response=response_data,  # Store actual response
            expected=FixtureExpected(
                status=status,
                data=response_data if 200 <= status < 400 else None,
                error_contains=error_contains,
                note=note,
            ),
        )

        # Generate filename
        if filename is None and self.auto_name:
            filename = self._auto_filename(name)

        # Validate filename exists
        if filename is None:
            raise ValueError(
                "filename is required when auto_name=False. Either provide a filename "
                "parameter or enable auto_name=True in FixtureGenerator initialization."
            )

        # Determine save path
        if subdirectory:
            save_dir = self.output_dir / subdirectory
            save_dir.mkdir(parents=True, exist_ok=True)
            save_path = subdirectory + "/" + filename
        else:
            save_path = filename

        # Save fixture
        self.loader.save(fixture, save_path)

        full_path = self.output_dir / save_path
        return full_path

    async def capture_batch(
        self,
        test_cases: list[dict[str, Any]],
        subdirectory: str | None = None,
    ) -> list[Path]:
        """
        Capture multiple test cases at once.

        Args:
            test_cases: List of test case configs (each dict passed to capture_response)
            subdirectory: Optional subdirectory for all fixtures

        Returns:
            List of paths to saved fixtures

        Example:
            ```python
            paths = await generator.capture_batch([
                {
                    "name": "List refunds - empty",
                    "endpoint": "/refunds",
                    "params": {"from_date": "2015/05/01"}
                },
                {
                    "name": "List refunds - with results",
                    "endpoint": "/refunds",
                    "params": {"from_date": "2024/01/01"}
                },
                {
                    "name": "List refunds - unauthorized",
                    "endpoint": "/refunds",
                    "override_auth": "INVALID"
                },
            ], subdirectory="refunds")
            ```
        """
        paths = []

        for test_case in test_cases:
            # Create a copy to avoid mutating caller's data
            test_case_copy = test_case.copy()

            # Add subdirectory if not already specified
            if subdirectory and "subdirectory" not in test_case_copy:
                test_case_copy["subdirectory"] = subdirectory

            path = await self.capture_response(**test_case_copy)
            paths.append(path)

        return paths

    async def capture_error_scenarios(
        self,
        endpoint: str,
        method: str = "GET",
        base_params: dict[str, Any] | None = None,
        subdirectory: str | None = None,
    ) -> list[Path]:
        """
        Capture common error scenarios for an endpoint.

        Captures:
        - 401 Unauthorized (invalid token)
        - 403 Forbidden (missing required param)
        - 404 Not Found (invalid endpoint)

        Args:
            endpoint: API endpoint path
            method: HTTP method
            base_params: Base parameters for the request
            subdirectory: Optional subdirectory for fixtures

        Returns:
            List of paths to saved error fixtures

        Example:
            ```python
            # Automatically capture common errors
            paths = await generator.capture_error_scenarios(
                endpoint="/v2/transactions/refunds",
                method="GET",
                base_params={"from_transaction_date": "2015/05/01"},
                subdirectory="refunds"
            )
            ```
        """
        endpoint_name = endpoint.strip("/").replace("/", "_")
        test_cases = []

        # 401 - Invalid auth token
        test_cases.append(
            {
                "name": f"{endpoint_name} - unauthorized",
                "endpoint": endpoint,
                "method": method,
                "params": base_params,
                "override_auth": "INVALID_TOKEN",
                "note": "Tests 401 Unauthorized with invalid token",
            }
        )

        # 404 - Invalid endpoint (if applicable)
        if "{" not in endpoint:  # Don't modify parameterized endpoints
            invalid_endpoint = endpoint.rstrip("/") + "/nonexistent"
            test_cases.append(
                {
                    "name": f"{endpoint_name} - not found",
                    "endpoint": invalid_endpoint,
                    "method": method,
                    "params": base_params,
                    "note": "Tests 404 Not Found with invalid resource",
                }
            )

        return await self.capture_batch(test_cases, subdirectory=subdirectory)

    def _auto_filename(self, name: str) -> str:
        """Generate a filename from test name."""
        # Convert to lowercase, replace spaces/special chars with underscores
        # Note: This removes slashes, so "/users - GET" becomes "users_get"
        filename = re.sub(r"[^\w\s-]", "", name.lower())
        filename = re.sub(r"[-\s]+", "_", filename)
        # Remove leading/trailing underscores
        filename = filename.strip("_")

        # If filename is empty (all special characters), use hash of original name
        if not filename:
            import hashlib

            name_hash = hashlib.md5(name.encode()).hexdigest()[:8]
            filename = f"fixture_{name_hash}"

        return f"{filename}.json"

    def _extract_error_messages(
        self, response_data: dict[str, Any] | list[Any] | str | int | float | bool | None
    ) -> list[str]:
        """Extract error messages from response for validation.

        Handles all valid JSON types that APIs might return as error responses.
        """
        messages = []

        # Handle different response types
        if isinstance(response_data, dict):
            # Dict response - extract from common error fields
            error_fields = ["error", "message", "detail", "error_description"]
            for field in error_fields:
                if field in response_data:
                    value = response_data[field]
                    if isinstance(value, str):
                        messages.append(value)
                    elif isinstance(value, dict):
                        # Nested error object
                        messages.extend(self._extract_error_messages(value))

        elif isinstance(response_data, str):
            # String response - use entire string as error message
            if response_data:
                messages.append(response_data)

        elif isinstance(response_data, list):
            # Array response - extract from each item
            for item in response_data:
                if isinstance(item, dict):
                    messages.extend(self._extract_error_messages(item))
                elif isinstance(item, str) and item:
                    messages.append(item)

        # For other types (int, float, bool, None) - use generic error message
        return messages if messages else ["error"]

    def generate_test_file(
        self,
        fixtures: list[Path] | str,
        output_file: str,
        tool_name: str = "my_tool",
    ) -> None:
        """
        Generate a pytest test file from fixtures.

        Args:
            fixtures: List of fixture paths or glob pattern
            output_file: Path to output test file
            tool_name: Name of the MCP tool being tested

        Example:
            ```python
            # Generate test file from all refund fixtures
            generator.generate_test_file(
                fixtures="refunds/*.json",
                output_file="tests/test_refunds.py",
                tool_name="list_refunds"
            )
            ```
        """
        # Load fixtures and track their paths
        fixture_data: list[tuple[FixtureModel, str]] = []

        if isinstance(fixtures, str):
            # Load from glob pattern, tracking paths
            for fixture_file in self.output_dir.glob(fixtures):
                if fixture_file.is_file():
                    relative_path = str(fixture_file.relative_to(self.output_dir))
                    # Convert Windows backslashes to forward slashes for Python string compatibility
                    relative_path = relative_path.replace("\\", "/")
                    fixture_model = self.loader.load(relative_path)
                    fixture_data.append((fixture_model, relative_path))
        else:
            # Load from list of paths
            for f in fixtures:
                relative_path = str(f.relative_to(self.output_dir))
                # Convert Windows backslashes to forward slashes for Python string compatibility
                relative_path = relative_path.replace("\\", "/")
                fixture_model = self.loader.load(relative_path)
                fixture_data.append((fixture_model, relative_path))

        # Extract unique base endpoints (strip path parameters)
        unique_endpoints = set()
        for fixture, _ in fixture_data:
            # Get base endpoint (e.g., "/users" from "/users/123"
            # or "/v1/account" from "/v1/account/123")
            endpoint = fixture.request.endpoint.strip("/")

            if not endpoint:
                # Handle edge case: empty or root endpoint
                base_endpoint = "/"
            else:
                parts = endpoint.split("/")

                # Handle versioned APIs (v1, v2, etc.)
                if len(parts) >= 2 and parts[0].lower().startswith("v") and parts[0][1:].isdigit():
                    # Versioned API: use /v1/resource pattern
                    base_endpoint = f"/{parts[0]}/{parts[1]}"
                else:
                    # Non-versioned: use /resource pattern (first segment only)
                    base_endpoint = f"/{parts[0]}"

            unique_endpoints.add(base_endpoint)

        # Sort by length descending to ensure longer paths are matched first
        # This prevents /v1 from matching /v1/account requests
        unique_endpoints = sorted(unique_endpoints, key=lambda x: len(x), reverse=True)

        # Generate test code
        lines = [
            '"""Auto-generated acceptance tests from live API fixtures.',
            "",
            "These tests call your MCP tools and make detailed assertions",
            "based on actual API responses captured from the live API.",
            '"""',
            "",
            "import pytest",
            "from typing import Any",
            "",
            "",
            "@pytest.fixture",
            f"def {tool_name}():",
            '    """Router that dispatches API calls to your MCP tools.',
            "    ",
            "    TODO: Import your tools and map them to endpoints below.",
            "    ",
            "    Your API has these endpoints:",
        ]

        # Add comment showing all endpoints that need mapping
        for endpoint in unique_endpoints:
            lines.append(f"    #   {endpoint}")

        lines.extend(
            [
                "    ",
                "    Common patterns:",
                "    1. One tool per endpoint (FactSet/ADP pattern):",
                "       from tools import get_users, get_posts",
                "       Map /users -> get_users, /posts -> get_posts",
                "    ",
                "    2. One router tool (QuickBooks pattern):",
                "       from tools import quickbooks",
                "       Return quickbooks tool that routes internally",
                '    """',
                "    # TODO: Import your tools here",
                "    # from mcp_servers.your_server.tools import (",
                "    #     get_users,  # handles /users endpoint",
                "    #     get_posts,  # handles /posts endpoint",
                "    # )",
                "    ",
                "    async def router(",
                "        method: str,",
                "        endpoint: str,",
                "        params: dict[str, Any] | None = None,",
                "        data: dict[str, Any] | None = None",
                "    ) -> dict[str, Any] | list[Any]:",
                '        """Route requests to appropriate tool based on endpoint."""',
                "        ",
                "        # TODO: Map endpoints to your tools",
            ]
        )

        # Generate if/elif chain for endpoint routing
        for i, endpoint in enumerate(unique_endpoints):
            # Generate valid Python identifier from endpoint
            # /v1/account -> account_tool, /users -> users_tool, / -> root_tool
            endpoint_clean = endpoint.strip("/")
            if endpoint_clean:
                tool_suggestion = endpoint_clean.split("/")[-1].replace("-", "_")
            else:
                tool_suggestion = "root"  # Handle root endpoint

            if i == 0:
                lines.append(f"        if endpoint.startswith({repr(endpoint)}):")
            else:
                lines.append(f"        elif endpoint.startswith({repr(endpoint)}):")
            error_msg = f"TODO: Import and call tool for {endpoint} endpoint"
            lines.extend(
                [
                    f"            # TODO: Call your tool for {endpoint}",
                    (
                        f"            # return await {tool_suggestion}_tool("
                        "method, endpoint, params, data)"
                    ),
                    "            raise NotImplementedError(",
                    f"                {repr(error_msg)}",
                    "            )",
                ]
            )

        # Only add else clause if there were endpoints to generate if/elif for
        if unique_endpoints:
            lines.extend(
                [
                    "        else:",
                    '            raise ValueError(f"Unknown endpoint: {endpoint}")',
                ]
            )
        else:
            # No endpoints found - add a helpful error message
            lines.append('        raise NotImplementedError("No endpoints found in fixtures")')

        lines.extend(
            [
                "    ",
                "    return router",
                "",
                "",
            ]
        )

        # Generate test for each fixture
        used_test_names = set()
        for fixture, fixture_path in fixture_data:
            # Remove .json extension and sanitize for function name
            test_name = self._auto_filename(fixture.name).replace(".json", "")

            # Ensure test name is unique to avoid silent overwrites
            original_name = test_name
            counter = 2
            while test_name in used_test_names:
                test_name = f"{original_name}_{counter}"
                counter += 1
            used_test_names.add(test_name)

            # Handle raw JSON fixtures without expected field
            if fixture.expected:
                expected_status = fixture.expected.status
                expected_note = fixture.expected.note or fixture.name
                expected_error_contains = fixture.expected.error_contains
                expected_data = fixture.expected.data
            else:
                # Raw JSON fixture - use defaults
                expected_status = 200
                expected_note = fixture.name
                expected_error_contains = None
                expected_data = fixture.response

            # Determine if this is an error test (includes status=0 for connection errors)
            is_error_test = expected_status >= 400 or expected_status == 0

            # Build function signature and docstring
            # Sanitize note to avoid breaking triple-quoted docstrings
            # First escape backslashes, then replace triple quotes
            sanitized_note = expected_note.replace("\\", "\\\\")
            sanitized_note = sanitized_note.replace('"""', '"').replace("'''", "'")
            lines.extend(
                [
                    "@pytest.mark.asyncio",
                    f"async def test_{test_name}({tool_name}):",
                    f'    """{sanitized_note}"""',
                ]
            )

            # Build request parameters
            params_dict = fixture.request.params or {}
            body_dict = fixture.request.body or {}

            if is_error_test:
                # Error test - expect exception
                lines.extend(
                    [
                        "    # This test expects an error response",
                        "    with pytest.raises(Exception) as exc_info:",
                        f"        await {tool_name}(",
                        f"            method={repr(fixture.request.method)},",
                        f"            endpoint={repr(fixture.request.endpoint)},",
                    ]
                )

                if params_dict:
                    lines.append(f"            params={repr(params_dict)},")
                if body_dict:
                    lines.append(f"            data={repr(body_dict)},")

                lines.extend(
                    [
                        "        )",
                        "",
                        "    # Assert error status code (if available)",
                        "    exc = exc_info.value",
                        "    if hasattr(exc, 'status_code'):",
                        f"        assert exc.status_code == {expected_status}",
                        "    elif hasattr(exc, 'response') and hasattr(exc.response, "
                        "'status_code'):",
                        f"        assert exc.response.status_code == {expected_status}",
                    ]
                )

                # Add error message assertions if available
                if expected_error_contains:
                    for error_msg in expected_error_contains[:2]:  # First 2 error messages
                        lines.append(f"    assert {repr(error_msg)} in str(exc_info.value)")

            else:
                # Success test - check response
                lines.extend(
                    [
                        f"    resp = await {tool_name}(",
                        f"        method={repr(fixture.request.method)},",
                        f"        endpoint={repr(fixture.request.endpoint)},",
                    ]
                )

                if params_dict:
                    lines.append(f"        params={repr(params_dict)},")
                if body_dict:
                    lines.append(f"        data={repr(body_dict)},")

                lines.extend(
                    [
                        "    )",
                        "",
                        "    # Assert response structure and data",
                    ]
                )

                # Generate assertions based on response type
                if expected_data is None:
                    # No expected data specified - just verify call succeeded
                    lines.append("    # TODO: Add assertions for response data")
                    lines.append("    assert resp is not None")
                elif isinstance(expected_data, dict):
                    lines.append("    assert isinstance(resp, dict)")
                    # Add assertions for top-level keys
                    for key in list(expected_data.keys())[:5]:  # First 5 keys
                        lines.append(f"    assert {repr(key)} in resp")
                elif isinstance(expected_data, list):
                    lines.append("    assert isinstance(resp, list)")
                    if expected_data:
                        lines.append(f"    # Response contains {len(expected_data)} items")
                        if len(expected_data) > 0 and isinstance(expected_data[0], dict):
                            # Guard against empty list before accessing resp[0]
                            lines.append("    assert len(resp) > 0, 'Expected non-empty list'")
                            # Add assertions for first item's keys
                            for key in list(expected_data[0].keys())[:5]:  # First 5 keys
                                lines.append(f"    assert {repr(key)} in resp[0]")
                else:
                    # Scalar values (strings, numbers, booleans)
                    lines.append(f"    assert resp == {repr(expected_data)}")

            lines.extend(["", ""])  # Blank lines between tests

        # Write file
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines))
