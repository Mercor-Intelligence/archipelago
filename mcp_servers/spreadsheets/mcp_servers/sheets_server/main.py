"""Sheets MCP Server.

Tool registration is controlled by the USE_INDIVIDUAL_TOOLS environment variable:
- USE_INDIVIDUAL_TOOLS=true (default): 12 individual tools for UI display
- USE_INDIVIDUAL_TOOLS=false: 2 meta-tools for LLM agents

Meta-tools:
| Tool          | Actions                                                              |
|---------------|----------------------------------------------------------------------|
| sheets        | help, create, delete, read_tab, read_csv, list_tabs, add_tab,        |
|               | delete_tab, edit, add_content, delete_content, create_chart,         |
|               | filter_tab                                                           |
| sheets_schema | Get JSON schema for any input/output model                           |

Individual tools:
- create_spreadsheet, delete_spreadsheet, read_tab, read_csv, list_tabs_in_spreadsheet
- add_tab, delete_tab, edit_spreadsheet, add_content_text, delete_content_cell, create_chart
- filter_tab
"""

import asyncio
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from mcp_actor.paths import ActorMiddleware
from mcp_schema import flatten_schema
from middleware.envelope_compat import EnvelopeCompatMiddleware
from middleware.injected_errors import setup_error_injection
from middleware.logging import LoggingMiddleware
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

mcp = FastMCP(
    "sheets-server",
    instructions="Excel (.xlsx) spreadsheets in a sandboxed directory. Create workbooks, read/write cells, manage sheets, formulas, and charts; read CSV from the same root. Use for reports, tabular analysis, and training agents on spreadsheet workflows.",
)
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ActorMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())
mcp.add_middleware(EnvelopeCompatMiddleware(mcp))

# Mutually exclusive: USE_INDIVIDUAL_TOOLS gets individual tools, otherwise meta-tools
if os.getenv("USE_INDIVIDUAL_TOOLS", "").lower() in ("true", "1", "yes"):
    # Register individual tools (11 tools for UI)
    from models.response import (
        AddContentTextOutput,
        AddTabOutput,
        CreateChartOutput,
        CreateSpreadsheetOutput,
        DeleteContentCellOutput,
        DeleteSpreadsheetOutput,
        DeleteTabOutput,
        EditSpreadsheetOutput,
        FilterTabOutput,
        ListTabsOutput,
        ReadCsvOutput,
        ReadTabOutput,
    )
    from tools.add_content_text import add_content_text
    from tools.add_tab import add_tab
    from tools.create_chart import create_chart
    from tools.create_spreadsheet import create_spreadsheet
    from tools.delete_content_cell import delete_content_cell
    from tools.delete_spreadsheet import delete_spreadsheet
    from tools.delete_tab import delete_tab
    from tools.edit_spreadsheet import edit_spreadsheet
    from tools.filter_tab import filter_tab
    from tools.list_tabs_in_spreadsheet import list_tabs_in_spreadsheet
    from tools.read_csv import read_csv
    from tools.read_tab import read_tab
    from utils.structured_output import with_structured_output

    # Each tool keeps its exact text result and additionally returns typed
    # structuredContent matching the declared outputSchema.
    for tool_fn, output_model in (
        (create_spreadsheet, CreateSpreadsheetOutput),
        (delete_spreadsheet, DeleteSpreadsheetOutput),
        (read_tab, ReadTabOutput),
        (read_csv, ReadCsvOutput),
        (list_tabs_in_spreadsheet, ListTabsOutput),
        (add_tab, AddTabOutput),
        (delete_tab, DeleteTabOutput),
        (edit_spreadsheet, EditSpreadsheetOutput),
        (add_content_text, AddContentTextOutput),
        (delete_content_cell, DeleteContentCellOutput),
        (create_chart, CreateChartOutput),
        (filter_tab, FilterTabOutput),
    ):
        mcp.tool(
            with_structured_output(tool_fn),
            output_schema=output_model.model_json_schema(),
        )
else:
    # Register meta-tools (2 tools instead of 11)
    from tools._meta_tools import sheets, sheets_schema

    mcp.tool(sheets)
    mcp.tool(sheets_schema)


async def _flatten_tool_schemas():
    """Flatten all registered tool parameter schemas for runtime compatibility.

    Output schemas are deliberately left as standard JSON Schema: clients
    validate structuredContent against them with ``jsonschema``, and the
    flattener rewrites ``anyOf: [X, null]`` to ``nullable: true``, which
    ``jsonschema`` ignores, so null fields would fail validation.
    """
    for tool in await mcp.list_tools():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            tool.parameters = flatten_schema(params)


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
    # Set up error injection middleware if config exists
    setup_error_injection(mcp)

    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
