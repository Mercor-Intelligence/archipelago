"""Sheets MCP Server.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 2 meta-tools for LLM agents
- GUI_ENABLED=true: 11 individual tools for UI display

Meta-tools:
| Tool          | Actions                                                              |
|---------------|----------------------------------------------------------------------|
| sheets        | help, create, delete, read_tab, read_csv, list_tabs, add_tab,        |
|               | delete_tab, edit, add_content, delete_content, create_chart          |
| sheets_schema | Get JSON schema for any input/output model                           |

Individual tools:
- create_spreadsheet, delete_spreadsheet, read_tab, read_csv, list_tabs_in_spreadsheet
- add_tab, delete_tab, edit_spreadsheet, add_content_text, delete_content_cell, create_chart
"""

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware

mcp = FastMCP("sheets-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    # Register individual tools (11 tools for UI)
    from tools.add_content_text import add_content_text
    from tools.add_tab import add_tab
    from tools.create_chart import create_chart
    from tools.create_spreadsheet import create_spreadsheet
    from tools.delete_content_cell import delete_content_cell
    from tools.delete_spreadsheet import delete_spreadsheet
    from tools.delete_tab import delete_tab
    from tools.edit_spreadsheet import edit_spreadsheet
    from tools.list_tabs_in_spreadsheet import list_tabs_in_spreadsheet
    from tools.read_csv import read_csv
    from tools.read_tab import read_tab

    mcp.tool(create_spreadsheet)
    mcp.tool(delete_spreadsheet)
    mcp.tool(read_tab)
    mcp.tool(read_csv)
    mcp.tool(list_tabs_in_spreadsheet)
    mcp.tool(add_tab)
    mcp.tool(delete_tab)
    mcp.tool(edit_spreadsheet)
    mcp.tool(add_content_text)
    mcp.tool(delete_content_cell)
    mcp.tool(create_chart)
else:
    # Register meta-tools (2 tools instead of 11)
    from tools._meta_tools import sheets, sheets_schema

    mcp.tool(sheets)
    mcp.tool(sheets_schema)

if __name__ == "__main__":
    mcp.run()
