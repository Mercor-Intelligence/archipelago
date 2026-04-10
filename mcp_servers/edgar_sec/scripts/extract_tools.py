import asyncio
import json
import os
import sys
from pathlib import Path

# Set environment variables to ensure successful startup and individual tool extraction
# These must be set BEFORE importing main modules

# GUI_ENABLED and USE_INDIVIDUAL_TOOLS are set per-pass in main()

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
os.environ.setdefault("EDGAR_USER_AGENT", "ToolExtraction contact@example.com")

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
    """Extract tools from a server by importing and calling register functions."""
    try:
        # Import the server's main module
        sys.path.insert(0, server_path)

        # Also add parent directory to support package-style imports
        parent_path = str(Path(server_path).parent)
        if parent_path not in sys.path:
            sys.path.insert(0, parent_path)

        # Clear cached modules to avoid conflicts
        if "main" in sys.modules:
            del sys.modules["main"]

        import main  # noqa: E402, F811

        # Try multiple registration function patterns
        for func_name in ["register_tools", "_register_tools", "_register_ui_tools"]:
            if hasattr(main, func_name) and callable(getattr(main, func_name)):
                try:
                    getattr(main, func_name)()
                    break
                except Exception as e:
                    # Registration may fail during CI (e.g., missing env vars, database)
                    # but tools might still be auto-registered, so continue
                    print(
                        f"Warning: {func_name}() failed for {server_path}: {e}",
                        file=sys.stderr,
                    )

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
        # Also remove parent path
        parent_path = str(Path(server_path).parent)
        if parent_path in sys.path:
            sys.path.remove(parent_path)


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


# Run with proper event loop handling
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            # Event loop already running from imported modules
            # Get the running loop and run our coroutine
            loop = asyncio.get_event_loop()
            loop.run_until_complete(main())
        else:
            raise
