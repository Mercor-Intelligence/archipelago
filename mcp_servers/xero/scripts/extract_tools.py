import asyncio
import json
import os
import sys
from pathlib import Path

# Set environment variables to ensure individual tools are registered
# These must be set BEFORE importing main modules
# GUI_ENABLED and USE_INDIVIDUAL_TOOLS are set per-pass in main()

# Discover all server directories
mcp_servers_path = Path(__file__).parent.parent / "mcp_servers"
server_dirs = [
    d.name
    for d in mcp_servers_path.iterdir()
    if d.is_dir() and not d.name.startswith("_") and (d / "main.py").exists()
]

result = []


async def extract_from_server(server_path: str):
    """Extract tools from a server by importing and calling register_tools."""
    try:
        # Import the server's main module
        sys.path.insert(0, server_path)

        # Clear cached modules to avoid conflicts
        if "main" in sys.modules:
            del sys.modules["main"]

        import main  # noqa: E402, F811

        # Call register_tools() if it exists (for servers that use lazy registration)
        if hasattr(main, "register_tools") and callable(main.register_tools):
            try:
                main.register_tools()
            except Exception:
                # register_tools() may fail during CI (e.g., missing env vars, database)
                # but tools might still be auto-registered, so continue
                pass

        # Get the mcp instance
        if not hasattr(main, "mcp"):
            return []

        mcp_instance = main.mcp

        # Extract tools
        tools = await mcp_instance.get_tools()
        server_tools = []
        for tool in tools.values():
            entry = {"name": tool.name, "description": tool.description or ""}
            if hasattr(tool, "parameters") and tool.parameters:
                entry["inputSchema"] = tool.parameters
            if hasattr(tool, "output_schema") and tool.output_schema:
                entry["outputSchema"] = tool.output_schema
            server_tools.append(entry)
        return server_tools
    except Exception as e:
        # Server import/registration failed, skip it
        print(f"Warning: Failed to extract from {server_path}: {e}", file=sys.stderr)
        return []
    finally:
        # Remove server path to avoid conflicts
        if server_path in sys.path:
            sys.path.remove(server_path)


async def main():
    # Pass 1: Extract individual tools
    os.environ["GUI_ENABLED"] = "true"
    os.environ["USE_INDIVIDUAL_TOOLS"] = "true"

    for server_name in sorted(server_dirs):
        server_path = str(mcp_servers_path / server_name)
        tools = await extract_from_server(server_path)
        result.extend(tools)

    individual_names = {t["name"] for t in result}

    # Pass 2: Extract meta tools (only add tools not already in the individual set)
    os.environ["GUI_ENABLED"] = "false"
    os.environ.pop("USE_INDIVIDUAL_TOOLS", None)

    for server_name in sorted(server_dirs):
        server_path = str(mcp_servers_path / server_name)
        tools = await extract_from_server(server_path)
        for tool in tools:
            if tool["name"] not in individual_names:
                tool["metadata"] = {"is_meta_tool": True}
                result.append(tool)

    print(json.dumps(result))


asyncio.run(main())
