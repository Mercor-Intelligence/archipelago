import asyncio
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from mcp_schema import flatten_schema
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
from tools.get_directory_tree import get_directory_tree
from tools.get_file_metadata import get_file_metadata
from tools.list_files import list_files
from tools.read_image_file import read_image_file
from tools.read_text_file import read_text_file
from tools.search_files import search_files

mcp = FastMCP(
    "filesystem-server",
    instructions="Read-only access to a sandboxed directory. List and search files, read text or image files, get file or directory metadata, and get a directory tree. No create, modify, or delete. Use for browsing files, validating paths, and feeding content to vision or text agents.",
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

mcp.tool(list_files)
mcp.tool(read_image_file)
mcp.tool(read_text_file)
mcp.tool(search_files)
mcp.tool(get_file_metadata)
mcp.tool(get_directory_tree)


async def _flatten_tool_schemas():
    for tool in (await mcp.get_tools()).values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            tool.parameters = flatten_schema(params)
        output = getattr(tool, "output_schema", None)
        if isinstance(output, dict):
            tool.output_schema = flatten_schema(output)


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
