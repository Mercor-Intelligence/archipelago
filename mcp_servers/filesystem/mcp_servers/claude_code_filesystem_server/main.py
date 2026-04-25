import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
from tools.bash import bash
from tools.edit import edit
from tools.glob import glob
from tools.grep import grep
from tools.ls import ls
from tools.monitor import monitor
from tools.multi_edit import multi_edit
from tools.notebook_edit import notebook_edit
from tools.notebook_read import notebook_read
from tools.read import read
from tools.todo_read import todo_read
from tools.todo_write import todo_write
from tools.write import write

mcp = FastMCP(
    "claude-code-filesystem-server",
    instructions=(
        "Sandboxed filesystem and shell tools designed to replace Claude Code's built-in "
        "Bash, Read, Write, Edit, Glob, Grep, and LS tools. All file paths are resolved "
        "under APP_FS_ROOT (the sandbox root). Use these tools for all file and shell "
        "operations; the built-in Claude Code tools are disabled."
    ),
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

mcp.tool(bash)
mcp.tool(read)
mcp.tool(write)
mcp.tool(edit)
mcp.tool(multi_edit)
mcp.tool(glob)
mcp.tool(grep)
mcp.tool(ls)
mcp.tool(monitor)
mcp.tool(notebook_read)
mcp.tool(notebook_edit)
mcp.tool(todo_read)
mcp.tool(todo_write)

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
