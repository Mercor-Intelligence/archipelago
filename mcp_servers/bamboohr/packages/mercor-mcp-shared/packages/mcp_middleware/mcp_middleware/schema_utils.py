"""Schema utilities for MCP tool compatibility.

This module provides utilities for processing JSON schemas used by MCP tools,
particularly for compatibility with LLM providers that have specific schema requirements.

Note: Gemini compatibility is now handled by GeminiBaseModel in mcp_schema package.
Models that inherit from GeminiBaseModel automatically produce Gemini-compatible schemas.
"""

from typing import Any


def apply_default_setup(mcp: Any) -> None:
    """
    Apply all default setup and compatibility fixes to an MCP server.

    This function should be called after all tools are registered with the
    FastMCP instance. It applies standard configurations that all Mercor
    MCP servers should have.

    Note: Gemini compatibility is now handled automatically by GeminiBaseModel.
    Models that inherit from GeminiBaseModel produce flat schemas without
    $defs, $ref, or anyOf patterns.

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
    # Gemini compatibility is now handled by GeminiBaseModel in mcp_schema
    # Future default setup can be added here:
    # - Telemetry setup
    # - Default tool registration
    # - etc.
    pass
