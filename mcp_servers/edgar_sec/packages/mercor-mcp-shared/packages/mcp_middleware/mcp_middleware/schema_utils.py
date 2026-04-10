"""Schema utilities for MCP tool compatibility.

This module provides utilities for processing JSON schemas used by MCP tools,
particularly for compatibility with LLM providers that have specific schema requirements.
"""

import asyncio
from typing import Any

from mcp_schema import flatten_schema


async def _flatten_registered_tool_schemas(mcp: Any) -> None:
    """Flatten registered FastMCP tool schemas in place.

    Even when input/output models inherit from GeminiBaseModel, FastMCP may still
    generate wrapper schemas containing ``$defs``/``$ref`` for function parameters.
    This post-processing step ensures tool schemas exposed by ``tools/list`` are
    consistently flattened for LLM compatibility.
    """
    tools = await mcp.get_tools()
    for tool in tools.values():
        schema = getattr(tool, "parameters", None)
        if schema:
            tool.parameters = flatten_schema(schema)


def apply_default_setup(mcp: Any) -> None:
    """
    Apply all default setup and compatibility fixes to an MCP server.

    This function should be called after all tools are registered with the
    FastMCP instance. It applies standard configurations that all Mercor
    MCP servers should have.

    Note:
        GeminiBaseModel handles model-level flattening, but FastMCP tool wrapper
        schemas can still contain ``$defs``/``$ref``. This function normalizes
        registered tool schemas to keep ``tools/list`` output compatible.

    Args:
        mcp: The FastMCP instance with registered tools

    Example:
        >>> from fastmcp import FastMCP
        >>> from mcp_middleware import apply_default_setup
        >>>
        >>> mcp = FastMCP("MyServer")
        >>> mcp.tool(my_tool)
        >>> apply_default_setup(mcp)
        >>>
        >>> if __name__ == "__main__":
        >>>     mcp.run()
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running — safe to block until flattening completes.
        asyncio.run(_flatten_registered_tool_schemas(mcp))
        return

    raise RuntimeError(
        "apply_default_setup() must be called before the event loop starts "
        "(i.e. before run_server() / mcp.run()). "
        "Call it from synchronous setup code, not from within an async function."
    )
