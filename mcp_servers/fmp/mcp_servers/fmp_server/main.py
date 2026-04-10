"""
FMP MCP Server - Financial Modeling Prep API.

This server uses meta-tools pattern for LLM context optimization.
Instead of 179 individual tools, it exposes 10 domain-based meta-tools:

- fmp_analyst: Analyst ratings, grades, price targets (12 actions)
- fmp_prices: Stock quotes, historical data, intraday (26 actions)
- fmp_company: Company profiles, search, directory (35 actions)
- fmp_financials: Financial statements, DCF, earnings (44 actions)
- fmp_market: Market data, indexes, economics (25 actions)
- fmp_assets: ETFs, commodities, crypto, forex (12 actions)
- fmp_news: News and press releases (10 actions)
- fmp_government: Congressional trading, SEC filings (6 actions)
- fmp_technical: Technical indicators (9 actions)
- fmp_schema: Tool introspection (1 action)

Each meta-tool supports action="help" for discovery.

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 10 meta-tools for LLM agents
- GUI_ENABLED=true: 179 individual tools for UI display

Gemini Compatibility:
- All tool schemas have $defs removed and $ref resolved inline
- This prevents Gemini BadRequestError with $defs references
"""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)

# Support all execution methods:
# 1. python main.py (direct execution)
# 2. python -m mcp_servers.fmp_server (module execution)
# 3. import main (RLS wrapper from same directory)
try:
    from .middleware.logging import LoggingMiddleware
    from .middleware.response_truncation import ResponseTruncationMiddleware
    from .middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
    from .startup import startup_validation
    from .tools import (
        analyst,
        charts,
        commodity,
        company_information,
        company_search,
        congressional_trading,
        crypto,
        dcf_valuation,
        earnings_dividends_splits,
        earnings_transcript,
        economics,
        etf_mutual_funds,
        financial_statements,
        forex,
        indexes,
        market_hours,
        market_performance,
        news,
        quote,
        sec_filings,
        stock_directory,
        technical_indicators,
    )
    from .tools._meta_tools import (
        fmp_analyst,
        fmp_assets,
        fmp_company,
        fmp_financials,
        fmp_government,
        fmp_market,
        fmp_news,
        fmp_prices,
        fmp_schema,
        fmp_technical,
    )
    from .utils.schema import flatten_schema
except ImportError:
    # Direct execution - add parent to path for local imports
    sys.path.insert(0, str(Path(__file__).parent))
    from middleware.logging import LoggingMiddleware
    from middleware.response_truncation import ResponseTruncationMiddleware
    from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
    from startup import startup_validation
    from tools import (
        analyst,
        charts,
        commodity,
        company_information,
        company_search,
        congressional_trading,
        crypto,
        dcf_valuation,
        earnings_dividends_splits,
        earnings_transcript,
        economics,
        etf_mutual_funds,
        financial_statements,
        forex,
        indexes,
        market_hours,
        market_performance,
        news,
        quote,
        sec_filings,
        stock_directory,
        technical_indicators,
    )
    from tools._meta_tools import (
        fmp_analyst,
        fmp_assets,
        fmp_company,
        fmp_financials,
        fmp_government,
        fmp_market,
        fmp_news,
        fmp_prices,
        fmp_schema,
        fmp_technical,
    )
    from utils.schema import flatten_schema

mcp = FastMCP("fmp-server")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
_raw_max = os.getenv("MAX_RESPONSE_ITEMS", "200")
try:
    _max_items = max(1, int(_raw_max))
except ValueError:
    _max_items = 200
mcp.add_middleware(ResponseTruncationMiddleware(max_items=_max_items))
mcp.add_middleware(ValidationErrorSanitizerMiddleware())


# Monkey-patch FastMCP to sanitize tool schemas for Gemini compatibility
# Gemini rejects schemas with $defs/$ref in function responses
_original_tool_decorator = mcp.tool


def _sanitized_tool_decorator(func=None, **kwargs):
    """Wrapper that sanitizes tool schemas by removing $defs and resolving $ref."""
    if func is None:
        # Called with arguments: @mcp.tool(name="...")
        def decorator(f):
            return _sanitized_tool_decorator(f, **kwargs)

        return decorator

    # Register with original decorator
    result = _original_tool_decorator(func, **kwargs)

    # Sanitize the tool schema in the MCP tools registry
    # The tool name is either provided in kwargs or derived from function name
    tool_name = kwargs.get("name", func.__name__)

    # Access the internal tools registry and sanitize the schema
    # FastMCP stores tools in _tool_manager._tools, not directly in _tools
    tool_manager = getattr(mcp, "_tool_manager", None)
    if tool_manager is not None:
        tools_registry = getattr(tool_manager, "_tools", None)
        if tools_registry is not None and tool_name in tools_registry:
            tool_def = tools_registry[tool_name]
            # Sanitize the input schema if it exists
            if hasattr(tool_def, "input_schema") and tool_def.input_schema:
                if isinstance(tool_def.input_schema, dict):
                    tool_def.input_schema = flatten_schema(tool_def.input_schema.copy())

    return result


# Replace the tool decorator with our sanitized version
mcp.tool = _sanitized_tool_decorator


# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    # Register individual tools (179 tools for UI)
    ALL_TOOL_MODULES = [
        analyst,
        charts,
        commodity,
        company_information,
        company_search,
        congressional_trading,
        crypto,
        dcf_valuation,
        earnings_dividends_splits,
        earnings_transcript,
        economics,
        etf_mutual_funds,
        financial_statements,
        forex,
        indexes,
        market_hours,
        market_performance,
        news,
        quote,
        sec_filings,
        stock_directory,
        technical_indicators,
    ]

    for module in ALL_TOOL_MODULES:
        for name in dir(module):
            obj = getattr(module, name)
            if callable(obj) and not name.startswith("_") and hasattr(obj, "__module__"):
                if obj.__module__ == module.__name__:
                    mcp.tool(obj)
else:
    # Register meta-tools (10 tools instead of 179)
    mcp.tool(fmp_analyst)
    mcp.tool(fmp_prices)
    mcp.tool(fmp_company)
    mcp.tool(fmp_financials)
    mcp.tool(fmp_market)
    mcp.tool(fmp_assets)
    mcp.tool(fmp_news)
    mcp.tool(fmp_government)
    mcp.tool(fmp_technical)
    mcp.tool(fmp_schema)


async def _flatten_tool_schemas():
    """Flatten all registered tool parameter schemas for runtime compatibility."""
    for tool in (await mcp.get_tools()).values():
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
    # Do not schedule a background task while pytest-asyncio owns the loop: the task
    # often outlives individual tests and can block clean process shutdown (IDE runner
    # appears hung until Ctrl+C). Subprocess / production imports still create the task.
    if "pytest" not in sys.modules:
        _flatten_tool_schemas_task = loop.create_task(_flatten_tool_schemas())
        _flatten_tool_schemas_task.add_done_callback(_log_flatten_task_error)


if __name__ == "__main__":
    # Perform startup validation (fail-fast for offline mode)
    startup_validation()
    # Run with configurable transport
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port, show_banner=False)
    else:
        # Disable banner output to stdout - it breaks JSON-RPC protocol in STDIO transport
        mcp.run(transport="stdio", show_banner=False)
