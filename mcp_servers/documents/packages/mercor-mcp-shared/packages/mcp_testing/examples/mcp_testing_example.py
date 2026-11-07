"""Example: Universal MCP Testing with MCPClient and MCPValidator.

This example demonstrates the NEW universal testing workflow that works
with ANY MCP server, not just those wrapping REST APIs.

Perfect for testing MCP servers like FactSet that use mock data.
"""

import asyncio

from mcp_testing import MCPClient


async def example_direct_mcp_testing():
    """Example 1: Test MCP server directly via protocol."""
    print("=" * 70)
    print("Example 1: Direct MCP Tool Testing")
    print("=" * 70)

    # Create MCP client
    client = MCPClient(server_script="../../mcp_servers/spider_man_quote/main.py")

    # Call tool directly
    print("\nCalling spider_man_quote tool...")
    result = await client.call_tool("spider_man_quote", {})

    print(f"Result: {result}")
    print(f"Type: {type(result)}")

    # List available tools
    print("\nListing all available tools...")
    tools = await client.list_tools()
    print(f"Found {len(tools)} tools:")
    for tool in tools:
        print(f"  - {tool.get('name', 'unknown')}")


async def example_fixture_validation():
    """Example 2: Validate MCP tool against fixture data."""
    print("\n" + "=" * 70)
    print("Example 2: Fixture-Based Validation")
    print("=" * 70)

    # This would work with FactSet server and fixtures
    # (Commented out since we don't have access to FactSet repo here)
    """
    validator = MCPValidator(
        server_script="../../mcp_servers/factset/main.py",
        fixtures_dir="../../mcp_servers/factset/fixtures"
    )

    # Test that get_prices returns expected mock data
    result = await validator.validate_tool(
        tool_name="get_prices",
        tool_args={
            "ids": ["TSLA-US"],
            "startDate": "2025-01-01",
            "endDate": "2025-01-31",
            "frequency": "D",
            "currency": "USD"
        },
        expected_fixture="prices/tsla_ytd_prices.json"
    )

    if result["passed"]:
        print(" PASS: Tool output matches fixture!")
    else:
        print(" FAIL: Validation errors:")
        for error in result["errors"]:
            print(f"  - {error}")
    """

    print("\n(FactSet example commented out - requires FactSet server)")
    print("This would validate MCP tool outputs against fixture files")


async def example_raw_json_fixtures():
    """Example 3: Using raw JSON fixtures (not FixtureModel format)."""
    print("\n" + "=" * 70)
    print("Example 3: Raw JSON Fixture Support")
    print("=" * 70)

    # Create validator
    # Works with BOTH:
    # - Full FixtureModel files: {name, request, expected, response}
    # - Raw JSON data files: {"data": [...]}  (like FactSet fixtures)

    print("\nThe fixture loader now auto-detects format:")
    print("  - FixtureModel: {name, request, expected} -> loaded as-is")
    print("  - Raw JSON: {...} -> auto-wrapped as FixtureModel")
    print("\nThis means FactSet's existing fixture files work without changes!")


async def example_mock_first_workflow():
    """Example 4: Mock-first testing (no live API required)."""
    print("\n" + "=" * 70)
    print("Example 4: Mock-First Testing Workflow")
    print("=" * 70)

    print("\nOld workflow (requires live API):")
    print("  1. Get API key")
    print("  2. Capture fixtures from live API")
    print("  3. Implement MCP server")
    print("  4. Test MCP vs fixtures")

    print("\nNEW workflow (works with mock data):")
    print("  1. Create mock data fixtures")
    print("  2. Implement MCP server")
    print("  3. Test MCP directly with MCPClient")
    print("  4. Validate against fixtures with MCPValidator")

    print("\nNo live API required - perfect for FactSet scenario!")


async def main():
    """Run all examples."""
    print("\n")
    print("=" * 70)
    print("       MCP Testing - Universal Examples")
    print("=" * 70)

    try:
        await example_direct_mcp_testing()
    except Exception as e:
        print(f"Example 1 failed: {e}")

    await example_fixture_validation()
    await example_raw_json_fixtures()
    await example_mock_first_workflow()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("\nThe mcp_testing framework now supports:")
    print("  [+] Direct MCP protocol testing (MCPClient)")
    print("  [+] Fixture validation (MCPValidator)")
    print("  [+] Raw JSON fixtures (auto-detected)")
    print("  [+] Mock-first workflow (no live API needed)")
    print("  [+] Live API testing (original workflow)")
    print("\nIT'S NOW TRULY UNIVERSAL!")


if __name__ == "__main__":
    asyncio.run(main())
