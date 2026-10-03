"""MCP tool validator using fixtures.

Simple validator for testing MCP tools against fixture data.
Works with both live MCP servers and mock data.
"""

from pathlib import Path
from typing import Any

from .core.comparator import DEFAULT_IGNORE_FIELDS, DataComparator
from .core.models import Severity
from .fixtures.loader import FixtureLoader
from .mcp_client import MCPClient


class MCPValidator:
    """Validate MCP tool responses against fixture data.

    Example:
        ```python
        # Test MCP server with existing fixtures
        validator = MCPValidator(
            server_script="mcp_servers/factset/main.py",
            fixtures_dir="mcp_servers/factset/fixtures"
        )

        # Validate a tool against a fixture
        result = await validator.validate_tool(
            tool_name="get_prices",
            tool_args={"ids": ["TSLA-US"], "startDate": "2025-01-01"},
            expected_fixture="prices/tsla_ytd_prices.json"
        )

        assert result["passed"], f"Validation failed: {result['errors']}"
        ```
    """

    def __init__(
        self,
        server_script: str | Path | None = None,
        fixtures_dir: str | Path | None = None,
        mcp_client: MCPClient | None = None,
        strict_mode: bool = False,
    ):
        """Initialize MCP validator.

        Args:
            server_script: Path to MCP server main.py (creates MCPClient)
            fixtures_dir: Directory containing fixture files
            mcp_client: Pre-configured MCP client (alternative to server_script)
            strict_mode: If True, treat all differences as errors
        """
        if mcp_client:
            self.client = mcp_client
        elif server_script:
            self.client = MCPClient(server_script)
        else:
            raise ValueError("Must provide either server_script or mcp_client")

        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else None
        self.loader = FixtureLoader(fixtures_dir) if fixtures_dir else None
        self.strict_mode = strict_mode

    async def validate_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        expected_fixture: str | dict[str, Any] | None = None,
        expected_data: dict[str, Any] | None = None,
        ignore_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Validate an MCP tool's output against expected data.

        Args:
            tool_name: Name of the MCP tool to test
            tool_args: Arguments to pass to the tool
            expected_fixture: Path to fixture file (if using fixtures)
            expected_data: Expected data dict (alternative to fixture)
            ignore_fields: Fields to ignore in comparison. Common dynamic fields
                are ignored by default: ["timestamp", "id", "requestId", "created_at",
                "updated_at"]. Pass a custom list to override, or [] to ignore nothing.

        Returns:
            Validation result with keys:
                - passed (bool): Whether validation passed
                - actual: Actual tool output
                - expected: Expected output
                - errors: List of error messages (if any)

        Example:
            ```python
            # Using a fixture file (with default ignore_fields)
            result = await validator.validate_tool(
                "get_prices",
                {"ids": ["TSLA-US"]},
                expected_fixture="prices/tsla_ytd_prices.json"
            )

            # Ignore nothing - validate all fields
            result = await validator.validate_tool(
                "get_prices",
                {"ids": ["TSLA-US"]},
                expected_data={"data": [...]},
                ignore_fields=[]  # Validate everything
            )
            ```
        """
        if ignore_fields is None:
            ignore_fields = DEFAULT_IGNORE_FIELDS.copy()

        # Load expected data
        if expected_fixture is not None:
            if isinstance(expected_fixture, str):
                # Load from file
                if self.loader is None:
                    raise ValueError(
                        "Cannot load fixture from path: fixtures_dir was not provided during "
                        "initialization. Either provide fixtures_dir in __init__ or pass "
                        "expected_data as a dict instead of a path string."
                    )
                fixture = self.loader.load(expected_fixture)
                # For error fixtures, expected.data is None and actual data is in fixture.response
                expected = (
                    fixture.expected.data
                    if (fixture.expected and fixture.expected.data is not None)
                    else fixture.response
                )
            else:
                expected = expected_fixture
        elif expected_data is not None:
            expected = expected_data
        else:
            raise ValueError("Must provide either expected_fixture or expected_data")

        # Call MCP tool
        try:
            actual = await self.client.call_tool(tool_name, tool_args)
        except Exception as e:
            return {
                "passed": False,
                "actual": None,
                "expected": expected,
                "errors": [f"Tool execution failed: {str(e)}"],
            }

        # Compare actual vs expected using DataComparator
        comparator = DataComparator(ignore_fields=ignore_fields, strict_mode=self.strict_mode)
        differences = comparator.compare_fields(expected, actual)

        # Convert Difference objects to error strings (only ERROR severity counts as failure)
        errors = []
        for diff in differences:
            # Only report ERROR severity differences as errors
            if diff.severity == Severity.ERROR:
                if diff.message:
                    errors.append(f"{diff.path}: {diff.message}")
                else:
                    errors.append(
                        f"{diff.path}: {diff.type.value} - "
                        f"expected {diff.expected}, got {diff.actual}"
                    )

        return {
            "passed": len(errors) == 0,
            "actual": actual,
            "expected": expected,
            "errors": errors,
        }

    async def validate_all_fixtures(
        self,
        tool_name: str,
        fixture_pattern: str = "**/*.json",
        ignore_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Validate a tool against all matching fixtures.

        Args:
            tool_name: Name of the MCP tool
            fixture_pattern: Glob pattern for fixtures
            ignore_fields: Fields to ignore in comparison

        Returns:
            Summary with total/passed/failed counts and details
        """
        if not self.loader:
            raise ValueError("fixtures_dir must be set to use validate_all_fixtures")

        fixtures = self.loader.load_all(fixture_pattern)
        results = []

        for fixture in fixtures:
            # Extract tool args from fixture request (merge params and body)
            tool_args = {}
            if fixture.request.params:
                tool_args.update(fixture.request.params)
            if fixture.request.body:
                tool_args.update(fixture.request.body)

            # For error fixtures, expected.data is None and actual data is in fixture.response
            expected_data = (
                fixture.expected.data
                if (fixture.expected and fixture.expected.data is not None)
                else fixture.response
            )

            result = await self.validate_tool(
                tool_name=tool_name,
                tool_args=tool_args,
                expected_data=expected_data,
                ignore_fields=ignore_fields,
            )

            results.append(
                {
                    "fixture": fixture.name,
                    "passed": result["passed"],
                    "errors": result["errors"],
                }
            )

        passed = sum(1 for r in results if r["passed"])
        failed = len(results) - passed

        return {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "results": results,
        }
