"""Calendar MCP Server.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 2 meta-tools for LLM agents
- GUI_ENABLED=true: 5 individual tools for UI display

Meta-tools:
| Tool            | Actions                                       |
|-----------------|-----------------------------------------------|
| calendar        | list, read, create, update, delete            |
| calendar_schema | Get JSON schema for any input/output model    |

Individual tools:
- list_events, read_event, create_event, update_event, delete_event
"""

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware

mcp = FastMCP("calendar-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    # Register individual tools (5 tools for UI)
    from tools.create_event import create_event
    from tools.delete_event import delete_event
    from tools.list_events import list_events
    from tools.read_event import read_event
    from tools.update_event import update_event

    mcp.tool(list_events)
    mcp.tool(read_event)
    mcp.tool(create_event)
    mcp.tool(update_event)
    mcp.tool(delete_event)
else:
    # Register meta-tools (2 tools instead of 5)
    from tools._meta_tools import calendar, calendar_schema

    mcp.tool(calendar)
    mcp.tool(calendar_schema)

if __name__ == "__main__":
    mcp.run()
