"""Example: Testing FactSet MCP Server with Universal Testing Framework.

This example shows how to test an MCP server with mock data (no live API).
Works with the FactSet server in mercor-factset repository.
"""

import asyncio
import sys
from pathlib import Path

# Add factset server to path if available
factset_path = Path.home() / "Documents" / "mercor-factset" / "mcp_servers" / "factset"
if factset_path.exists():
    sys.path.insert(0, str(factset_path.parent.parent))

from mcp_testing import MCPClient, MCPValidator  # noqa: E402


async def test_factset_server():
    """Test FactSet MCP server with its mock fixtures."""
    print("=" * 70)
    print("Testing FactSet MCP Server")
    print("=" * 70)

    # Check if FactSet server exists
    server_path = factset_path / "main.py"
    fixtures_dir = factset_path / "fixtures"

    if not server_path.exists():
        print(f"\n[!] FactSet server not found at: {server_path}")
        print("This example requires the mercor-factset repository.")
        return

    print(f"\n[+] Found server: {server_path}")
    print(f"[+] Found fixtures: {fixtures_dir}")

    # Create MCP client
    print("\n" + "-" * 70)
    print("1. Testing Direct MCP Tool Calls")
    print("-" * 70)

    client = MCPClient(server_script=server_path)

    # List available tools
    print("\nDiscovering tools...")
    try:
        tools = await client.list_tools()
        print(f"Found {len(tools)} tools:")
        for tool in tools:
            print(f"  - {tool.get('name', 'unknown')}")
    except Exception as e:
        print(f"Could not list tools: {e}")
        print("(This is expected - may require server protocol updates)")

    # Test a tool directly
    print("\n" + "-" * 70)
    print("2. Calling get_prices Tool")
    print("-" * 70)

    try:
        result = await client.call_tool(
            "get_prices",
            {
                "ids": ["TSLA-US"],
                "startDate": "2025-01-01",
                "endDate": "2025-01-31",
                "frequency": "D",
                "currency": "USD",
            },
        )
        print("\n[+] Tool executed successfully!")
        print(f"Response type: {type(result)}")
        print(f"Response preview: {str(result)[:200]}...")
    except Exception as e:
        print(f"\n[!] Tool call failed: {e}")
        print("(This may require MCP protocol implementation in server)")

    # Validate against fixtures
    print("\n" + "-" * 70)
    print("3. Validating Against Fixtures")
    print("-" * 70)

    try:
        validator = MCPValidator(
            server_script=server_path,
            fixtures_dir=fixtures_dir,
        )

        result = await validator.validate_tool(
            tool_name="get_prices",
            tool_args={
                "ids": ["TSLA-US"],
                "startDate": "2025-01-01",
                "endDate": "2025-01-31",
            },
            expected_fixture="prices/tsla_ytd_prices.json",
            ignore_fields=["timestamp", "requestId"],
        )

        if result["passed"]:
            print("\n[+] PASS: Tool output matches fixture!")
        else:
            print("\n[-] FAIL: Validation errors:")
            for error in result["errors"]:
                print(f"  - {error}")

            print(f"\nExpected: {result['expected']}")
            print(f"Actual: {result['actual']}")

    except Exception as e:
        print(f"\n[!] Validation failed: {e}")

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    print("\nThe universal testing framework allows you to:")
    print("  1. Test MCP tools directly via protocol (no HTTP)")
    print("  2. Use existing mock fixture files (raw JSON)")
    print("  3. Validate tool outputs match expected data")
    print("  4. No live API access required!")


async def main():
    """Run FactSet testing example."""
    await test_factset_server()


if __name__ == "__main__":
    asyncio.run(main())
