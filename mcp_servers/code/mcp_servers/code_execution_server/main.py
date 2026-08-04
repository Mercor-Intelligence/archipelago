import asyncio
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from mcp_schema import flatten_schema
from middleware.injected_errors import setup_error_injection
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
from tools.code_exec import code_exec, verify_sandbox_available

mcp = FastMCP(
    "code-execution-server",
    instructions=(
        "Sandboxed execution of shell commands and Python in a persistent directory "
        "(APP_FS_ROOT). Run scripts, create/modify/delete files, install packages "
        "(e.g. pip/uv); configurable command timeout. No persistent Python session "
        "across calls. Use for data analysis, scripting, and training agents on code "
        "execution."
    ),
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

mcp.tool(code_exec)


async def _flatten_tool_schemas():
    # fastmcp 3.x ``list_tools()`` returns fresh Tool copies, so assigning to
    # their attributes never persists. Resolve the canonical registered tool
    # and mutate its cached schema dicts in place (the copies every later
    # ``list_tools()`` regenerates from) — the mercor-rls-pdf reference fix.
    for tool in await mcp.list_tools():
        canonical = await mcp.get_tool(tool.name)
        params = getattr(canonical, "parameters", None)
        if isinstance(params, dict):
            flattened = flatten_schema(params)
            params.clear()
            params.update(flattened)
        output = getattr(canonical, "output_schema", None)
        if isinstance(output, dict):
            flattened_output = flatten_schema(output)
            output.clear()
            output.update(flattened_output)


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
    verify_sandbox_available()
    setup_error_injection(mcp)
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
