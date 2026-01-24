import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware
from tools.get_directory_tree import get_directory_tree
from tools.get_file_metadata import get_file_metadata
from tools.list_files import list_files
from tools.read_image_file import read_image_file
from tools.read_text_file import read_text_file
from tools.search_files import search_files

mcp = FastMCP("filesystem-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

enabled_tools = os.getenv("TOOLS", "").split(",")
enabled_tools = [t.strip() for t in enabled_tools if t.strip()]

if not enabled_tools or "list_files" in enabled_tools:
    mcp.tool(list_files)
if not enabled_tools or "read_image_file" in enabled_tools:
    mcp.tool(read_image_file)
if not enabled_tools or "read_text_file" in enabled_tools:
    mcp.tool(read_text_file)
if not enabled_tools or "search_files" in enabled_tools:
    mcp.tool(search_files)
if not enabled_tools or "get_file_metadata" in enabled_tools:
    mcp.tool(get_file_metadata)
if not enabled_tools or "get_directory_tree" in enabled_tools:
    mcp.tool(get_directory_tree)

if __name__ == "__main__":
    mcp.run()
