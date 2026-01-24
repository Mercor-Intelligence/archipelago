"""PDF MCP Server.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 2 meta-tools for LLM agents
- GUI_ENABLED=true: 5 individual tools for UI display

Meta-tools:
| Tool       | Actions                                                    |
|------------|------------------------------------------------------------|
| pdf        | help, create, read_pages, read_image, page_as_image, search|
| pdf_schema | Get JSON schema for any input/output model                 |

Individual tools:
- create_pdf
- read_pdf_pages
- read_image
- read_page_as_image
- search_pdf
"""

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware

mcp = FastMCP("pdf-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    # Register individual tools (5 tools for UI)
    from tools.create_pdf import create_pdf
    from tools.read_image import read_image
    from tools.read_page_as_image import read_page_as_image
    from tools.read_pdf_pages import read_pdf_pages
    from tools.search_pdf import search_pdf

    mcp.tool(create_pdf)
    mcp.tool(read_pdf_pages)
    mcp.tool(read_image)
    mcp.tool(read_page_as_image)
    mcp.tool(search_pdf)
else:
    # Register meta-tools (2 tools instead of 5)
    from tools._meta_tools import pdf, pdf_schema

    mcp.tool(pdf)
    mcp.tool(pdf_schema)

if __name__ == "__main__":
    mcp.run()
