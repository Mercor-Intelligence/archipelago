"""Chat MCP Server.

Tool registration is controlled by the USE_INDIVIDUAL_TOOLS environment variable:
- USE_INDIVIDUAL_TOOLS=true (default): 9 individual tools for UI display
- USE_INDIVIDUAL_TOOLS=false: 2 meta-tools for LLM agents

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
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fastmcp as _fastmcp_mod
from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from mcp_schema import flatten_schema
from middleware.injected_errors import setup_error_injection
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

# Startup diagnostics — write to stderr so the canary can capture them
# even when stdout scrolls off the 2000-line container log dump.
_start_time = time.time()
print(f"[chat-diag] Starting chat server (pid={os.getpid()})", file=sys.stderr)
print(f"[chat-diag] fastmcp version: {_fastmcp_mod.__version__}", file=sys.stderr)
print(f"[chat-diag] MCP_PORT={os.getenv('MCP_PORT', 'not set')}", file=sys.stderr)
print(
    f"[chat-diag] MCP_TRANSPORT={os.getenv('MCP_TRANSPORT', 'not set')}",
    file=sys.stderr,
)
print(
    f"[chat-diag] USE_INDIVIDUAL_TOOLS={os.getenv('USE_INDIVIDUAL_TOOLS', 'not set')}",
    file=sys.stderr,
)


async def _flatten_tool_schemas(server: FastMCP) -> None:
    # fastmcp 3.x ``list_tools()`` returns fresh Tool copies, so assigning to
    # their attributes never persists. Resolve the canonical registered tool
    # and mutate its cached schema dicts in place (the copies every later
    # ``list_tools()`` regenerates from) — the mercor-rls-pdf reference fix.
    for tool in await server.list_tools():
        canonical = await server.get_tool(tool.name)
        params = getattr(canonical, "parameters", None)
        if isinstance(params, dict):
            flattened = flatten_schema(params)
            params.clear()
            params.update(flattened)


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Run loop-safe startup work inside the server's own event loop.

    Schema flattening must happen within the running event loop managed by the
    transport (streamable-HTTP / stdio). Doing it at import time via
    ``asyncio.run`` spins a competing loop that corrupts the HTTP transport
    session ("Server disconnected without sending a response").
    """
    print(
        f"[chat-diag] Lifespan starting ({time.time() - _start_time:.1f}s after import)",
        file=sys.stderr,
    )
    try:
        await _flatten_tool_schemas(server)
        tool_count = len(await server.list_tools())
        print(
            f"[chat-diag] Lifespan complete: {tool_count} tools flattened",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[chat-diag] Lifespan FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        raise
    yield {}
    print("[chat-diag] Lifespan shutdown", file=sys.stderr)


mcp = FastMCP(
    "chat-server",
    instructions=(
        "Mattermost/Slack-like messaging: channels (groups/spaces), threaded replies, "
        "emoji reactions. Post messages, reply in threads, browse channel history, add "
        "reactions, soft-delete posts. Current user identity is set via environment "
        "(e.g. CURRENT_USER_EMAIL). Data stored in JSON under a configurable root; no "
        "external chat APIs. Use for team chat simulation and training agents on "
        "channel-based communication."
    ),
    lifespan=_lifespan,
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

# Set up error injection middleware for Dynamic Friction testing
setup_error_injection(mcp)

# Mutually exclusive: USE_INDIVIDUAL_TOOLS gets individual tools, otherwise meta-tools
if os.getenv("USE_INDIVIDUAL_TOOLS", "").lower() in ("true", "1", "yes"):
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
    print("[chat-diag] Registered 9 individual tools", file=sys.stderr)
else:
    # Register meta-tools (2 tools instead of 9)
    from tools._meta_tools import chat, chat_schema

    mcp.tool(chat)
    mcp.tool(chat_schema)
    print("[chat-diag] Registered 2 meta-tools", file=sys.stderr)

print(
    f"[chat-diag] Module import complete ({time.time() - _start_time:.1f}s)",
    file=sys.stderr,
)

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        print(f"[chat-diag] Starting HTTP transport on port {port}", file=sys.stderr)
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        print("[chat-diag] Starting stdio transport", file=sys.stderr)
        mcp.run(transport="stdio")
