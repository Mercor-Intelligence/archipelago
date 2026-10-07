import asyncio
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from mcp_schema import flatten_schema
from middleware.actor import PathModeActorMiddleware
from middleware.injected_errors import setup_error_injection
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
from tools.get_directory_tree import DirectoryTreeResult, get_directory_tree
from tools.get_file_metadata import FileMetadataResult, get_file_metadata
from tools.list_files import ListFilesResult, list_files
from tools.read_image_file import read_image_file
from tools.read_text_file import ReadTextFileResult, read_text_file
from tools.search_files import SearchFilesResult, search_files
from utils.structured_output import output_schema, structured_tool

mcp = FastMCP(
    "filesystem-server",
    instructions="Read-only access to a sandboxed directory. List and search files, read text or image files, get file or directory metadata, and get a directory tree. No create, modify, or delete. Use for browsing files, validating paths, and feeding content to vision or text agents.",
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(PathModeActorMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

# Text tools are registered through structured_tool: the text content block is
# exactly what the function returns, and the same facts are published as typed
# structuredContent under the declared outputSchema. read_image_file returns
# image content and is registered as-is.
mcp.tool(structured_tool(list_files), output_schema=output_schema(ListFilesResult))
mcp.tool(read_image_file)
mcp.tool(
    structured_tool(read_text_file), output_schema=output_schema(ReadTextFileResult)
)
mcp.tool(structured_tool(search_files), output_schema=output_schema(SearchFilesResult))
mcp.tool(
    structured_tool(get_file_metadata),
    output_schema=output_schema(FileMetadataResult),
)
mcp.tool(
    structured_tool(get_directory_tree),
    output_schema=output_schema(DirectoryTreeResult),
)


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
    setup_error_injection(mcp)
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
