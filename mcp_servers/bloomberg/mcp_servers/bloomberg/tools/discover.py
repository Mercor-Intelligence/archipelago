"""Bloomberg discovery tool for LLM introspection.

Provides a lightweight way for LLMs to discover available Bloomberg tools
and understand their capabilities without needing action="help" on each tool.
"""

from mcp_schema import GeminiBaseModel
from pydantic import BaseModel, Field


class DiscoverInput(GeminiBaseModel):
    """Input for bloomberg_discover tool."""

    tool_name: str | None = Field(
        default=None,
        description="Optional: Get detailed info for a specific tool. Leave empty to list all tools.",
    )


class ToolInfo(BaseModel):
    """Information about a single tool."""

    name: str
    description: str
    required_params: list[str]
    optional_params: list[str]
    example_use: str


class DiscoverOutput(BaseModel):
    """Output from bloomberg_discover tool."""

    tools: list[ToolInfo] | None = None
    tool_detail: ToolInfo | None = None
    tip: str = ""


# Tool catalog - single source of truth for discovery
BLOOMBERG_TOOLS = {
    "reference_data": ToolInfo(
        name="reference_data",
        description="Get current quotes and reference data for securities. Use for real-time prices, company info, and fundamental data fields.",
        required_params=["securities", "fields"],
        optional_params=["overrides"],
        example_use='reference_data(securities=["AAPL US Equity"], fields=["PX_LAST", "NAME", "MARKET_CAP"])',
    ),
    "historical_data": ToolInfo(
        name="historical_data",
        description="Get historical OHLCV data for securities. Use for price history, charts, and backtesting.",
        required_params=["securities", "fields", "start_date", "end_date"],
        optional_params=["periodicity", "periodicity_adjustment", "currency"],
        example_use='historical_data(securities=["AAPL US Equity"], fields=["PX_LAST"], start_date="2024-01-01", end_date="2024-12-01")',
    ),
    "intraday_bars": ToolInfo(
        name="intraday_bars",
        description="Get intraday OHLCV bar data at various intervals (1min, 5min, etc.). Use for intraday analysis.",
        required_params=["security", "event_type", "interval", "start_date_time", "end_date_time"],
        optional_params=[],
        example_use='intraday_bars(security="AAPL US Equity", event_type="TRADE", interval=5, start_date_time="2024-01-15T09:30:00", end_date_time="2024-01-15T16:00:00")',
    ),
    "intraday_ticks": ToolInfo(
        name="intraday_ticks",
        description="Get intraday tick-level data. Use for high-frequency analysis and trade-by-trade data.",
        required_params=["security", "event_types", "start_date_time", "end_date_time"],
        optional_params=["include_condition_codes", "include_exchange_codes"],
        example_use='intraday_ticks(security="AAPL US Equity", event_types=["TRADE"], start_date_time="2024-01-15T09:30:00", end_date_time="2024-01-15T10:00:00")',
    ),
    "equity_screening": ToolInfo(
        name="equity_screening",
        description="Screen equities by criteria (sector, market cap, etc.). Use for finding stocks matching specific criteria.",
        required_params=["screen_type"],
        optional_params=["sector", "industry", "country", "market_cap_min", "market_cap_max"],
        example_use='equity_screening(screen_type="EQUITY", sector="Technology", market_cap_min=1000000000)',
    ),
    "list_symbols": ToolInfo(
        name="list_symbols",
        description="List all available symbols in the local database. Use to see what data is available offline.",
        required_params=[],
        optional_params=[],
        example_use="list_symbols()",
    ),
    "data_status": ToolInfo(
        name="data_status",
        description="Get status of locally cached data. Shows what symbols have data and date ranges.",
        required_params=[],
        optional_params=[],
        example_use="data_status()",
    ),
    "download_symbol": ToolInfo(
        name="download_symbol",
        description="Download historical data for a symbol to local cache. Use to populate offline data.",
        required_params=["symbol"],
        optional_params=["start_date", "end_date"],
        example_use='download_symbol(symbol="AAPL US Equity", start_date="2024-01-01")',
    ),
}


async def bloomberg_discover(request: DiscoverInput) -> DiscoverOutput:
    """Discover available Bloomberg tools and their capabilities."""
    if request.tool_name:
        # Return detail for specific tool
        tool = BLOOMBERG_TOOLS.get(request.tool_name)
        if not tool:
            return DiscoverOutput(
                tip=f"Unknown tool: {request.tool_name}. Available tools: {list(BLOOMBERG_TOOLS.keys())}"
            )
        return DiscoverOutput(
            tool_detail=tool, tip=f"Use {tool.name}() with the required parameters shown above."
        )

    # Return all tools
    return DiscoverOutput(
        tools=list(BLOOMBERG_TOOLS.values()),
        tip="Call bloomberg_discover(tool_name='<name>') for detailed info on any tool.",
    )
