"""Docs MCP Server.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 2 meta-tools for LLM agents
- GUI_ENABLED=true: 15 individual tools for UI display

Meta-tools:
| Tool        | Actions                                                              |
|-------------|----------------------------------------------------------------------|
| docs        | help, create, delete, overview, read_content, read_image, add_text,  |
|             | edit_text, delete_text, add_image, modify_image, format,             |
|             | header_footer, page_margins, page_orientation, comments              |
| docs_schema | Get JSON schema for any input/output model                           |

Individual tools:
- create_document, delete_document, get_document_overview, read_document_content
- read_image, add_content_text, edit_content_text, delete_content_text
- add_image, modify_image, apply_formatting, header_footer, page_margins
- page_orientation, comments
"""

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware

mcp = FastMCP("docs-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    # Register individual tools (15 tools for UI)
    from tools.add_content_text import add_content_text
    from tools.add_image import add_image
    from tools.apply_formatting import apply_formatting
    from tools.comments import comments
    from tools.create_document import create_document
    from tools.delete_content_text import delete_content_text
    from tools.delete_document import delete_document
    from tools.edit_content_text import edit_content_text
    from tools.get_document_overview import get_document_overview
    from tools.header_footer import header_footer
    from tools.modify_image import modify_image
    from tools.page_margins import page_margins
    from tools.page_orientation import page_orientation
    from tools.read_document_content import read_document_content
    from tools.read_image import read_image

    mcp.tool(create_document)
    mcp.tool(delete_document)
    mcp.tool(get_document_overview)
    mcp.tool(read_document_content)
    mcp.tool(read_image)
    mcp.tool(add_content_text)
    mcp.tool(edit_content_text)
    mcp.tool(delete_content_text)
    mcp.tool(add_image)
    mcp.tool(modify_image)
    mcp.tool(apply_formatting)
    mcp.tool(header_footer)
    mcp.tool(page_margins)
    mcp.tool(page_orientation)
    mcp.tool(comments)
else:
    # Register meta-tools (2 tools instead of 11)
    from tools._meta_tools import docs, docs_schema

    mcp.tool(docs)
    mcp.tool(docs_schema)

if __name__ == "__main__":
    mcp.run()
