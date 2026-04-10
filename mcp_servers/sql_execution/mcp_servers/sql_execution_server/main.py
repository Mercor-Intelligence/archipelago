import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
from tools.sql_exec import execute_sql

mcp = FastMCP(
    "sql-execution-server",
    instructions="""Execute SQL commands against a persistent SQLite database.

GETTING STARTED - Always discover the schema first:
1. List all tables: SELECT name FROM sqlite_master WHERE type='table'
2. Get table columns: PRAGMA table_info(table_name)
3. Preview data: SELECT * FROM table_name LIMIT 5

CAPABILITIES:
- SELECT queries with JOINs, subqueries, CTEs, aggregations, window functions
- INSERT, UPDATE, DELETE for data modification
- DDL: CREATE TABLE, DROP TABLE, ALTER TABLE, CREATE INDEX

LIMITATIONS:
- Single SQLite database only
- One SQL statement per call (no semicolon-separated batches)
- No stored procedures
- Use LIMIT for large result sets to avoid slow responses

DATA TYPES:
- INTEGER, REAL, TEXT, BLOB
- Dates: Store as TEXT in 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS' format
- Booleans: Use INTEGER (0=false, 1=true)
- JSON: Store as TEXT, use json_extract() to query

NOTE: The database may contain pre-loaded tables from CSV files.
Always discover existing tables before creating new ones.
""",
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

mcp.tool(execute_sql)

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
