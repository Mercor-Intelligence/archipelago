"""Mail MCP Server.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 2 meta-tools for LLM agents
- GUI_ENABLED=true: 7 individual tools for UI display

Meta-tools:
| Tool        | Actions                                                       |
|-------------|---------------------------------------------------------------|
| mail        | list, read, search, send, reply, reply_all, forward           |
| mail_schema | Get JSON schema for any input/output model                    |

Individual tools:
- list_mails, read_mail, search_mail, send_mail
- reply_mail, reply_all_mail, forward_mail
"""

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware

mcp = FastMCP("mail-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    # Register individual tools (7 tools for UI)
    from tools.forward_mail import forward_mail
    from tools.list_mails import list_mails
    from tools.read_mail import read_mail
    from tools.reply_all_mail import reply_all_mail
    from tools.reply_mail import reply_mail
    from tools.search_mail import search_mail
    from tools.send_mail import send_mail

    mcp.tool(list_mails)
    mcp.tool(read_mail)
    mcp.tool(search_mail)
    mcp.tool(send_mail)
    mcp.tool(reply_mail)
    mcp.tool(reply_all_mail)
    mcp.tool(forward_mail)
else:
    # Register meta-tools (2 tools instead of 7)
    from tools._meta_tools import mail, mail_schema

    mcp.tool(mail)
    mcp.tool(mail_schema)

if __name__ == "__main__":
    mcp.run()
