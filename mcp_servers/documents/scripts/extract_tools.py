import asyncio
import json
import os
import sys
from pathlib import Path

# Set environment variables to ensure successful startup and individual tool extraction
# These must be set BEFORE importing main modules

# Force individual tools (not meta-tools)
os.environ["GUI_ENABLED"] = "true"
os.environ["USE_INDIVIDUAL_TOOLS"] = "true"

# Set offline mode for all services to avoid API key requirements
os.environ["OFFLINE_MODE"] = "true"
os.environ["TERRAPIN_OFFLINE"] = "1"
os.environ["DATAGOV_OFFLINE_MODE"] = "true"
os.environ["XERO_OFFLINE_MODE"] = "true"
os.environ["CANVAS_OFFLINE"] = "true"
os.environ["EDGAR_OFFLINE_MODE"] = "true"
os.environ["FMP_OFFLINE_MODE"] = "true"
os.environ["COURT_LISTENER_OFFLINE"] = "true"

# Set dummy API keys to prevent startup failures (won't be used in offline mode)
os.environ.setdefault("TERRAPIN_API_KEY", "dummy_key_for_extraction")
os.environ.setdefault("FMP_API_KEY", "dummy_key_for_extraction")
os.environ.setdefault("XERO_CLIENT_ID", "dummy_client_id")
os.environ.setdefault("XERO_CLIENT_SECRET", "dummy_secret")
os.environ.setdefault("CANVAS_API_KEY", "dummy_key")
os.environ.setdefault("DATAGOV_API_KEY", "dummy_key")
os.environ.setdefault("SEARCH_MCP_GOOGLE_API_KEY", "dummy_key_for_extraction")
os.environ.setdefault("SEARCH_MCP_GOOGLE_CSE_ID", "dummy_cse_id_for_extraction")

# Set database URLs to in-memory to avoid file system dependencies
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

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
            except Exception as e:
                # register_tools() may fail during CI (e.g., missing env vars, database)
                # but tools might still be auto-registered, so continue
                print(
                    f"Warning: register_tools() failed for {server_path}: {e}",
                    file=sys.stderr,
                )

        # Get the mcp instance
        if not hasattr(main, "mcp"):
            return []

        mcp_instance = main.mcp

        # Extract tools
        tools = await mcp_instance.list_tools()
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
    for server_name in sorted(server_dirs):
        server_path = str(mcp_servers_path / server_name)
        tools = await extract_from_server(server_path)
        result.extend(tools)

    print(json.dumps(result))


asyncio.run(main())
