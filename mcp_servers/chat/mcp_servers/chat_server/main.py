"""Chat MCP Server.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 2 meta-tools for LLM agents
- GUI_ENABLED=true: 9 individual tools for UI display

Meta-tools:
| Tool        | Actions                                                               |
|-------------|-----------------------------------------------------------------------|
| chat        | list_channels, get_history, get_replies, get_user, get_users,         |
|             | post_message, reply_to_thread, add_reaction, delete_post              |
| chat_schema | Get JSON schema for any input/output model                            |

Individual tools:
- list_channels, get_channel_history, get_thread_replies
- get_user_profile, get_users, post_message
- reply_to_thread, add_reaction, delete_post
"""

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware

mcp = FastMCP("chat-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    # Register individual tools (9 tools for UI)
    from tools.add_reaction import add_reaction
    from tools.delete_post import delete_post
    from tools.get_channel_history import get_channel_history
    from tools.get_thread_replies import get_thread_replies
    from tools.get_user_profile import get_user_profile
    from tools.get_users import get_users
    from tools.list_channels import list_channels
    from tools.post_message import post_message
    from tools.reply_to_thread import reply_to_thread

    mcp.tool(list_channels)
    mcp.tool(get_channel_history)
    mcp.tool(get_thread_replies)
    mcp.tool(get_user_profile)
    mcp.tool(get_users)
    mcp.tool(post_message)
    mcp.tool(reply_to_thread)
    mcp.tool(add_reaction)
    mcp.tool(delete_post)
else:
    # Register meta-tools (2 tools instead of 9)
    from tools._meta_tools import chat, chat_schema

    mcp.tool(chat)
    mcp.tool(chat_schema)

if __name__ == "__main__":
    mcp.run()
