"""Pydantic models for Bloomberg MCP tools."""

from typing import Literal

from mcp_schema import GeminiBaseModel
from pydantic import BaseModel, Field

# Valid sector values for equity screening
ValidSector = Literal[
    "all",
    "energy",
    "materials",
    "industrials",
    "consumer_cyclical",
    "consumer_defensive",
    "financial",
    "healthcare",
    "technology",
    "communication_services",
    "utilities",
    "real_estate",
]


# Reference Data Models
class ReferenceDataInput(GeminiBaseModel):
    """Input for reference data tool."""

    securities: list[str] = Field(
        description="""List of securities in Bloomberg format.
IMPORTANT: You must use the full Bloomberg identifier format, NOT just the ticker symbol.

Format patterns:
- US Equities: "{TICKER} US Equity" (e.g., "AAPL US Equity", "MSFT US Equity")
- International Equities: "{TICKER} {EXCHANGE} Equity" (e.g., "ULVR LN Equity", "NESN SW Equity")
- Indices: "{TICKER} Index" (e.g., "SPX Index", "INDU Index")
- Bonds: "{CUSIP/ISIN} Corp" or "{TICKER} {COUPON} {MATURITY} Corp"
- Currencies: "{PAIR} Curncy" (e.g., "EURUSD Curncy")

WRONG: ["AAPL", "MSFT", "GOOGL"] - these will fail with INVALID_SECURITY error!
CORRECT: ["AAPL US Equity", "MSFT US Equity", "GOOGL US Equity"]

Maximum 50 securities per request.""",
        examples=[["AAPL US Equity", "MSFT US Equity"]],
    )
    fields: list[str] = Field(
        description="""List of Bloomberg field mnemonics to retrieve.

SUPPORTED FIELDS (use these):
- Price: PX_LAST (last price), PX_OPEN, PX_HIGH, PX_LOW
- Volume: VOLUME, PX_VOLUME
- Company: NAME, TICKER, EXCH_CODE, COUNTRY

NOT SUPPORTED (will fail with INVALID_FIELDS error):
- YLD_YTM_MID, G_SPD (bond yield fields)
- TOT_RETURN_INDEX_GROSS_DVDS (total return index)
- EQY_SH_OUT, BS_SH_OUT (shares outstanding - limited support)
- ENTERPRISE_VALUE, NET_DEBT (not available in offline mode)
- CUR_MKT_CAP (limited support)

Use the bloomberg_list_symbols tool to check available data first.""",
        examples=[["PX_LAST", "NAME", "VOLUME"]],
    )


class ReferenceDataOutput(BaseModel):
    """Output from reference data tool."""

    responses: list[dict] = Field(description="List of response envelopes")
    count: int = Field(description="Number of responses")


# Historical Data Models
class HistoricalDataInput(GeminiBaseModel):
    """Input for historical data tool."""

    securities: list[str] = Field(
        description="""List of securities in Bloomberg format.
IMPORTANT: Use full Bloomberg identifiers, NOT plain ticker symbols.

Format patterns:
- US Equities: "AAPL US Equity" (not "AAPL")
- Indices: "SPX Index" (not "^GSPC" or "SPX")
- Currencies: "EURUSD Curncy" (not "EUR/USD")

WRONG: ["AAPL", "MSFT"] - will fail with INVALID_SECURITY error!
CORRECT: ["AAPL US Equity", "MSFT US Equity"]

Maximum 50 securities per request.""",
        examples=[["AAPL US Equity", "MSFT US Equity"]],
    )
    fields: list[str] = Field(
        description="""List of Bloomberg field mnemonics for historical data.

FULLY SUPPORTED (recommended):
- PX_LAST: Last/closing price (adjusted or unadjusted based on adjustment_split/adjustment_normal parameters)
- PX_OPEN: Opening price
- PX_HIGH: High price
- PX_LOW: Low price
- VOLUME: Trading volume

APPROXIMATED (calculated, use with caution):
- VWAP: Calculated as (high + low + close) / 3

NOT SUPPORTED (will return fieldExceptions):
- YLD_YTM_MID, G_SPD: Bond yield fields not available
- TOT_RETURN_INDEX_GROSS_DVDS: Total return not available
- TRADE_COUNT, NUM_TRADES: Not available for historical data
- BID, ASK: Not available for historical (intraday only)

To get UNADJUSTED prices, set adjustment_split=false and adjustment_normal=false.""",
        examples=[["PX_LAST", "VOLUME", "PX_OPEN", "PX_HIGH", "PX_LOW"]],
    )
    start_date: str = Field(
        description="Start date in ISO format (use the date picker in UI, or format: 2025-11-01T00:00:00Z without quotes)",
        examples=["2025-11-01T00:00:00Z"],
    )
    end_date: str = Field(
        description="End date in ISO format (use the date picker in UI, or format: 2025-11-07T00:00:00Z without quotes)",
        examples=["2025-11-07T00:00:00Z"],
    )
    adjustment_split: bool = Field(
        default=True,
        description="Adjust prices for stock splits. Set to false for unadjusted (raw) prices.",
    )
    adjustment_normal: bool = Field(
        default=True,
        description="Adjust prices for normal cash dividends. Set to false for unadjusted prices.",
    )
    adjustment_abnormal: bool = Field(
        default=False,
        description="Adjust prices for special/abnormal dividends (e.g., special one-time dividends).",
    )


class HistoricalDataOutput(BaseModel):
    """Output from historical data tool."""

    responses: list[dict] = Field(description="List of response envelopes")
    count: int = Field(description="Number of responses")


# Intraday Bars Models
class IntradayBarsInput(GeminiBaseModel):
    """Input for intraday bars tool."""

    security: str = Field(
        description="""Single security in Bloomberg format.
IMPORTANT: Use the full Bloomberg identifier, NOT just the ticker symbol.

Example: "AAPL US Equity" (not just "AAPL")""",
        examples=["AAPL US Equity"],
    )
    start_datetime: str = Field(
        description="Start datetime in ISO format (use the date/time picker in UI, or format: 2025-11-25T09:30:00Z without quotes)",
        examples=["2025-11-25T09:30:00Z"],
    )
    end_datetime: str = Field(
        description="End datetime in ISO format (use the date/time picker in UI, or format: 2025-11-25T16:00:00Z without quotes)",
        examples=["2025-11-25T16:00:00Z"],
    )
    interval: int = Field(
        default=60, description="Bar interval in minutes (e.g., 1, 5, 15, 60)", ge=1, le=1440
    )


class IntradayBarsOutput(BaseModel):
    """Output from intraday bars tool."""

    responses: list[dict] = Field(description="List of response envelopes")
    count: int = Field(description="Number of responses")


# Intraday Ticks Models
class IntradayTicksInput(GeminiBaseModel):
    """Input for intraday ticks tool."""

    security: str = Field(
        description="""Single security in Bloomberg format.
IMPORTANT: Use the full Bloomberg identifier, NOT just the ticker symbol.

Example: "AAPL US Equity" (not just "AAPL")""",
        examples=["AAPL US Equity"],
    )
    start_datetime: str = Field(
        description="Start datetime in ISO format (use the date/time picker in UI, or format: 2025-11-25T09:30:00Z without quotes)",
        examples=["2025-11-25T09:30:00Z"],
    )
    end_datetime: str = Field(
        description="End datetime in ISO format (use the date/time picker in UI, or format: 2025-11-25T16:00:00Z without quotes)",
        examples=["2025-11-25T16:00:00Z"],
    )
    event_types: list[str] = Field(
        default=["TRADE"], description='Event types to retrieve (e.g., ["TRADE", "BID", "ASK"])'
    )


class IntradayTicksOutput(BaseModel):
    """Output from intraday ticks tool."""

    responses: list[dict] = Field(description="List of response envelopes")
    count: int = Field(description="Number of responses")


# Equity Screening (BEQS) Models
class EquityScreeningInput(GeminiBaseModel):
    """Input for equity screening (BEQS) tool."""

    screen_name: str = Field(default="Custom Screen", description="Name of the equity screen")
    sector: ValidSector | None = Field(
        default=None,
        description='''Sector filter for screening. Must be one of the exact lowercase values below.

VALID VALUES (use exactly as shown):
- "all" - All sectors
- "energy" - Energy sector
- "materials" - Materials sector
- "industrials" - Industrials (NOT "Industrial")
- "consumer_cyclical" - Consumer Discretionary (NOT "Consumer Discretionary")
- "consumer_defensive" - Consumer Staples
- "financial" - Financial Services (NOT "Financials")
- "healthcare" - Healthcare (NOT "Health Care")
- "technology" - Technology/IT (NOT "Information Technology")
- "communication_services" - Communication Services (NOT "Telecommunications" or "Telecom")
- "utilities" - Utilities
- "real_estate" - Real Estate

COMMON MISTAKES TO AVOID:
- "Telecommunications" → use "communication_services"
- "Industrial" → use "industrials" (lowercase, with 's')
- "Consumer Discretionary" → use "consumer_cyclical"
- "Financials" → use "financial"
- "Health Care" → use "healthcare"''',
    )
    market_cap_min: float | None = Field(
        default=None, description="Minimum market cap in millions USD", ge=0
    )
    market_cap_max: float | None = Field(
        default=None, description="Maximum market cap in millions USD", ge=0
    )


class EquityScreeningOutput(BaseModel):
    """Output from equity screening tool."""

    responses: list[dict] = Field(description="List of response envelopes")
    count: int = Field(description="Number of responses")


# Data Management Models
class DownloadSymbolInput(GeminiBaseModel):
    """Input for download_symbol tool."""

    symbol: str = Field(description='Stock ticker symbol (e.g., "AAPL")', examples=["AAPL"])
    data_type: str = Field(
        default="historical",
        description='Type of data: "historical" for daily OHLCV, or intraday like "intraday_5min", "intraday_15min", "intraday_1hour"',
        examples=["historical", "intraday_5min", "intraday_1hour"],
    )
    start_date: str | None = Field(
        default=None,
        description='Optional start date filter (e.g., "2024-01-01")',
        examples=["2024-01-01"],
    )
    end_date: str | None = Field(
        default=None,
        description='Optional end date filter (e.g., "2024-12-31")',
        examples=["2024-12-31"],
    )


class DownloadSymbolOutput(BaseModel):
    """Output from download_symbol tool."""

    symbol: str = Field(description="Stock ticker symbol")
    data_type: str = Field(description="Type of data returned")
    row_count: int = Field(description="Number of rows in CSV")
    csv_content: str = Field(description="CSV data with headers")


class ListSymbolsOutput(BaseModel):
    """Output from list_symbols tool."""

    symbols: list[str] = Field(description="List of available symbols")
    count: int = Field(description="Number of symbols")


class DataStatusOutput(BaseModel):
    """Output from data_status tool."""

    db_path: str = Field(description="Path to the database file")
    db_size_mb: float = Field(description="Database file size in MB")
    historical: dict | None = Field(default=None, description="Historical data range info")
    intraday: dict | None = Field(default=None, description="Intraday data ranges by interval")
    profiles_count: int | None = Field(default=None, description="Number of company profiles")
