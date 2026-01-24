import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware
from tools.code_exec import code_exec, verify_proot_available

mcp = FastMCP("code-execution-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

enabled_tools = os.getenv("TOOLS", "").split(",")
enabled_tools = [t.strip() for t in enabled_tools if t.strip()]

if not enabled_tools or "code_exec" in enabled_tools:
    mcp.tool(code_exec)

if __name__ == "__main__":
    verify_proot_available()
    mcp.run()
