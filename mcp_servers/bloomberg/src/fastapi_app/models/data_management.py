"""Pydantic models for data management MCP tools."""

from enum import Enum

from pydantic import BaseModel, Field


class DataType(str, Enum):
    """Types of data available for download."""

    HISTORICAL = "historical"
    INTRADAY_1MIN = "intraday_1min"
    INTRADAY_5MIN = "intraday_5min"
    INTRADAY_15MIN = "intraday_15min"
    INTRADAY_30MIN = "intraday_30min"
    INTRADAY_1HOUR = "intraday_1hour"
    INTRADAY_4HOUR = "intraday_4hour"


# -----------------------------------------------------------------------------
# Request Models
# -----------------------------------------------------------------------------


class DownloadSymbolRequest(BaseModel):
    """Request to download data for a single symbol."""

    symbol: str = Field(..., description="Stock ticker symbol (e.g., AAPL)")
    data_type: DataType = Field(
        DataType.HISTORICAL,
        description="Type of data: 'historical' for daily OHLCV, or intraday bars like 'intraday_5min', 'intraday_15min', 'intraday_1hour', etc.",
    )
    start_date: str | None = Field(
        default=None,
        description="Optional start date filter (e.g., '2024-01-01')",
    )
    end_date: str | None = Field(
        default=None,
        description="Optional end date filter (e.g., '2024-12-31')",
    )


# -----------------------------------------------------------------------------
# Response Models
# -----------------------------------------------------------------------------


class DateRange(BaseModel):
    """Date range for a data type."""

    first_date: str = Field(..., description="Earliest date in the data")
    last_date: str = Field(..., description="Most recent date in the data")
    row_count: int = Field(..., description="Number of rows")
    symbol_count: int = Field(..., description="Number of unique symbols")


class DataStatusResponse(BaseModel):
    """Response showing database status and date ranges."""

    db_path: str = Field(..., description="Path to the database file")
    db_size_mb: float = Field(..., description="Database file size in MB")
    historical: DateRange | None = Field(default=None, description="Historical data range")
    intraday_1min: DateRange | None = Field(default=None, description="1-minute bar range")
    intraday_5min: DateRange | None = Field(default=None, description="5-minute bar range")
    intraday_15min: DateRange | None = Field(default=None, description="15-minute bar range")
    intraday_30min: DateRange | None = Field(default=None, description="30-minute bar range")
    intraday_1hour: DateRange | None = Field(default=None, description="1-hour bar range")
    intraday_4hour: DateRange | None = Field(default=None, description="4-hour bar range")
    profiles_count: int | None = Field(default=None, description="Number of company profiles")


class ListSymbolsResponse(BaseModel):
    """Response listing available symbols."""

    symbols: list[str] = Field(..., description="List of available symbols")
    count: int = Field(..., description="Number of symbols")


class DownloadSymbolResponse(BaseModel):
    """Response containing CSV data for a symbol."""

    symbol: str = Field(..., description="Stock ticker symbol")
    data_type: DataType = Field(..., description="Type of data")
    row_count: int = Field(..., description="Number of rows in CSV")
    csv_content: str = Field(..., description="CSV data with headers")
