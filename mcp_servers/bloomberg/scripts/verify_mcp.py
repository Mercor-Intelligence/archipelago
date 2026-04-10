#!/usr/bin/env python3
"""
Verify Bloomberg MCP Server - Tool Registration Test
"""

import asyncio
import sys
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


async def verify_mcp():
    """Verify MCP server tools are registered correctly"""
    print("=" * 60)
    print("Bloomberg MCP Server - Verification")
    print("=" * 60)
    print()

    # Import MCP server
    try:
        from blpapi_mcp.main import mcp

        print("✅ MCP server imported successfully")
    except Exception as e:
        print(f"❌ Failed to import MCP server: {e}")
        return False

    # Check FastMCP instance
    try:
        print(f"✅ FastMCP instance: {mcp.name}")
    except Exception as e:
        print(f"❌ Failed to get FastMCP instance: {e}")
        return False

    # Get registered tools and resources using FastMCP API
    try:
        tools = await mcp.get_tools()
        print(f"\n✅ Registered Tools ({len(tools)}):")
        for tool in tools:
            tool_name = tool.name if hasattr(tool, "name") else str(tool)  # type: ignore[attr-defined]
            print(f"   - {tool_name}")
    except Exception as e:
        print(f"\n❌ Failed to get tools: {e}")
        tools = []

    try:
        resources = await mcp.get_resources()
        print(f"\n✅ Registered Resources ({len(resources)}):")
        for resource in resources:
            resource_name = resource.uri if hasattr(resource, "uri") else str(resource)  # type: ignore[attr-defined]
            print(f"   - {resource_name}")
    except Exception as e:
        print(f"\n❌ Failed to get resources: {e}")
        resources = []

    # Check for Bloomberg-specific tools
    bloomberg_tools = ["get_historical_data", "list_bloomberg_fields"]
    tool_names = [tool.name if hasattr(tool, "name") else str(tool) for tool in tools]  # type: ignore[attr-defined]
    missing_tools = [t for t in bloomberg_tools if t not in tool_names]

    if missing_tools:
        print(f"\n❌ Missing Bloomberg tools: {missing_tools}")
        return False

    print("\n" + "=" * 60)
    print("✅ All Bloomberg tools registered successfully!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    result = asyncio.run(verify_mcp())
    sys.exit(0 if result else 1)
