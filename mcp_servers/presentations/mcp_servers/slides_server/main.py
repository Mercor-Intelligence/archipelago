"""Slides MCP Server - Consolidated Meta-Tools Pattern.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 2 meta-tools for LLM agents
- GUI_ENABLED=true: 12 individual tools for UI display

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

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware

mcp = FastMCP("slides-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
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

if __name__ == "__main__":
    mcp.run()
