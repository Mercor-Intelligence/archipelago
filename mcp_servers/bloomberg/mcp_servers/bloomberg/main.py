import asyncio
import logging
import os
import sys
import warnings
from pathlib import Path

# Add paths before local imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages"))

# Suppress warnings before importing heavy libraries
warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore")

from fastmcp import FastMCP  # noqa: E402
from mcp_middleware.injected_errors import setup_error_injection  # noqa: E402
from mcp_schema.gemini import flatten_schema  # noqa: E402
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware  # noqa: E402

# Import tools (use absolute import to work with runpy)
from tools.bloomberg_tools import (  # noqa: E402
    data_status,
    download_symbol,
    equity_screening,
    historical_data,
    intraday_bars,
    intraday_ticks,
    list_symbols,
    reference_data,
)
from tools.discover import bloomberg_discover  # noqa: E402

# Configure logging to stderr to avoid breaking stdio JSON-RPC
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stderr)])
logger = logging.getLogger(__name__)

# Create MCP server
mcp = FastMCP(name="blpapi-mcp")
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

# Set up error injection middleware for Dynamic Friction testing
setup_error_injection(mcp)

# Parse enabled tools from environment
enabled_tools = os.getenv("TOOLS", "").split(",")
enabled_tools = [t.strip() for t in enabled_tools if t.strip()]

# Check if running in offline mode
is_offline_mode = os.getenv("MODE", "online").lower() == "offline"

# Register Bloomberg data tools
if not enabled_tools or "reference_data" in enabled_tools:
    mcp.tool(reference_data)
if not enabled_tools or "historical_data" in enabled_tools:
    mcp.tool(historical_data)
if not enabled_tools or "intraday_bars" in enabled_tools:
    mcp.tool(intraday_bars)
if not enabled_tools or "intraday_ticks" in enabled_tools:
    mcp.tool(intraday_ticks)
if not enabled_tools or "equity_screening" in enabled_tools:
    mcp.tool(equity_screening)

# Data management tools (only in offline mode - these read from local DuckDB)
if is_offline_mode:
    mcp.tool(list_symbols)
    mcp.tool(data_status)
    mcp.tool(download_symbol)
    logger.info("Offline mode: enabled list_symbols, data_status, download_symbol tools")
else:
    logger.info("Online mode: offline database tools disabled (use historical_data for live data)")

# Discovery tool (always enabled) - helps LLMs understand available tools
mcp.tool(bloomberg_discover)


async def _flatten_tool_schemas():
    """Flatten all registered tool parameter schemas for runtime compatibility."""
    for tool in (await mcp.get_tools()).values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            tool.parameters = flatten_schema(params)


_flatten_tool_schemas_task: asyncio.Task[None] | None = None


try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    asyncio.run(_flatten_tool_schemas())
else:
    _flatten_tool_schemas_task = loop.create_task(_flatten_tool_schemas())


def main():
    """Run MCP server with configurable transport."""
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    logger.info(f"Starting Bloomberg MCP server with {transport} transport")
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
