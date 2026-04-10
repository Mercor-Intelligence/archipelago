"""Slides MCP Server - Consolidated Meta-Tools Pattern.

Tool registration is controlled by the USE_INDIVIDUAL_TOOLS environment variable:
- USE_INDIVIDUAL_TOOLS=true (default): 12 individual tools for UI display
- USE_INDIVIDUAL_TOOLS=false: 2 meta-tools for LLM agents

Meta-tools:
| Tool          | Actions                                                              |
|---------------|----------------------------------------------------------------------|
| slides        | create, delete, add_slide, edit, add_image, modify_image,            |
|               | insert_chart, insert_table, read_range, read_deck, read_slide,       |
|               | read_image                                                           |
| slides_schema | Get JSON schema for any input/output model                           |

Individual tools:
- create_deck, delete_deck, add_slide, edit_slides
- add_image, modify_image, insert_chart, insert_table
- read_slides, read_completedeck, read_individualslide, read_image
"""

import asyncio
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

mcp = FastMCP(
    "slides-server",
    instructions="PowerPoint (.pptx) presentations in a sandboxed directory. Create decks, add/edit/delete slides, insert charts and tables, read content, extract images. Use for generating and editing presentations.",
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

# Mutually exclusive: USE_INDIVIDUAL_TOOLS gets individual tools, otherwise meta-tools
if os.getenv("USE_INDIVIDUAL_TOOLS", "").lower() in ("true", "1", "yes"):
    # Register individual tools (13 tools for UI)
    from tools.add_image import add_image
    from tools.add_shape import add_shape
    from tools.add_slide import add_slide
    from tools.create_slides import create_deck
    from tools.delete_slides import delete_deck
    from tools.edit_slides import edit_slides
    from tools.insert_chart import insert_chart
    from tools.insert_table import insert_table
    from tools.modify_image import modify_image
    from tools.read_completedeck import read_completedeck
    from tools.read_image import read_image
    from tools.read_individualslide import read_individualslide
    from tools.read_slides import read_slides

    mcp.tool(create_deck)
    mcp.tool(delete_deck)
    mcp.tool(add_slide)
    mcp.tool(edit_slides)
    mcp.tool(add_image)
    mcp.tool(modify_image)
    mcp.tool(insert_chart)
    mcp.tool(insert_table)
    mcp.tool(add_shape)
    mcp.tool(read_slides)
    mcp.tool(read_completedeck)
    mcp.tool(read_individualslide)
    mcp.tool(read_image)
else:
    # Register meta-tools (2 tools instead of 12)
    from tools._meta_tools import slides, slides_schema

    mcp.tool(slides)
    mcp.tool(slides_schema)


async def _flatten_tool_schemas() -> None:
    from mcp_schema import flatten_schema

    for tool in (await mcp.get_tools()).values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            tool.parameters = flatten_schema(params)
        output_schema = getattr(tool, "output_schema", None)
        if isinstance(output_schema, dict):
            tool.output_schema = flatten_schema(output_schema)


_flatten_tool_schemas_task: asyncio.Task[None] | None = None


def _log_flatten_task_error(task: asyncio.Task[None]) -> None:
    """Log background flatten errors without interrupting startup."""
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).error(
            "Background schema flattening failed: %s", exc, exc_info=True
        )


try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    asyncio.run(_flatten_tool_schemas())
else:
    _flatten_tool_schemas_task = loop.create_task(_flatten_tool_schemas())
    _flatten_tool_schemas_task.add_done_callback(_log_flatten_task_error)


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
