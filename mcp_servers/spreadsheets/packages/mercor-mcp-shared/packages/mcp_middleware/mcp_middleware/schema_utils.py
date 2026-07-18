"""Schema utilities for MCP tool compatibility.

This module provides utilities for processing JSON schemas used by MCP tools,
particularly for compatibility with LLM providers that have specific schema requirements.
"""

from typing import Any

from mcp_middleware.schema_flatten import SchemaFlattenMiddleware


def apply_default_setup(mcp: Any) -> None:
    """
    Apply all default setup and compatibility fixes to an MCP server.

    This function should be called after all tools are registered with the
    FastMCP instance. It applies standard configurations that all Mercor
    MCP servers should have.

    Note:
        ``GeminiBaseModel`` handles field-level annotation, but FastMCP tool
        wrapper schemas can still contain ``$defs`` / ``$ref`` / ``anyOf``.
        This registers :class:`SchemaFlattenMiddleware` so that both the runtime
        ``tools/list`` response (``on_list_tools``) and the direct
        ``list_tools()`` registry (``patch_tool_schemas``) expose flattened,
        Gemini-compatible INPUT schemas.

        A previous implementation mutated ``await mcp.list_tools()`` results at
        startup, but fastmcp returns fresh tool copies from that call, so the
        flatten was discarded and never served. The middleware approach fixes
        that and can be called before or after the event loop starts.

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
    flattener = SchemaFlattenMiddleware()
    mcp.add_middleware(flattener)
    flattener.patch_tool_schemas(mcp)
