import asyncio
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "mcp_servers", "filesystem_server")
)
from main import mcp  # noqa: E402


async def main():
    tools = await mcp.list_tools()
    result = []
    for tool in tools:
        entry = {"name": tool.name, "description": tool.description or ""}
        params = getattr(tool, "inputSchema", None) or getattr(tool, "parameters", None)
        if params:
            entry["inputSchema"] = params
        output = getattr(tool, "outputSchema", None) or getattr(
            tool, "output_schema", None
        )
        if output:
            entry["outputSchema"] = output
        result.append(entry)
    print(json.dumps(result))


asyncio.run(main())
