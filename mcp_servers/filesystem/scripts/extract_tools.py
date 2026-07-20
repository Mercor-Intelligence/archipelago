import asyncio
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "mcp_servers", "filesystem_server")
)
from main import mcp  # noqa: E402


async def main():
    tools = await mcp.get_tools()
    result = []
    for tool in tools.values():
        entry = {"name": tool.name, "description": tool.description or ""}
        if hasattr(tool, "parameters") and tool.parameters:
            entry["inputSchema"] = tool.parameters
        if hasattr(tool, "output_schema") and tool.output_schema:
            entry["outputSchema"] = tool.output_schema
        result.append(entry)
    print(json.dumps(result))


asyncio.run(main())
