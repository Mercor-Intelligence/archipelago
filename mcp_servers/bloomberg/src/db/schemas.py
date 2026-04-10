"""Pydantic schemas for database models."""

from datetime import date as DateType
from datetime import datetime

from pydantic import BaseModel, Field


class HistoricalPriceSchema(BaseModel):
    """Historical daily OHLCV price data."""

    symbol: str = Field(..., description="Stock ticker symbol")
    date: DateType = Field(..., description="Trading date")
    open: float | None = Field(None, description="Opening price")
    high: float | None = Field(None, description="High price")
    low: float | None = Field(None, description="Low price")
    close: float | None = Field(None, description="Closing price")
    adj_close: float | None = Field(None, description="Adjusted closing price")
    volume: int | None = Field(None, description="Trading volume")


class IntradayBarSchema(BaseModel):
    """Intraday OHLCV bar data."""

    symbol: str = Field(..., description="Stock ticker symbol")
    timestamp: datetime = Field(..., description="Bar timestamp")
    open: float | None = Field(None, description="Opening price")
    high: float | None = Field(None, description="High price")
    low: float | None = Field(None, description="Low price")
    close: float | None = Field(None, description="Closing price")
    volume: int | None = Field(None, description="Trading volume")


class SeedMetadataSchema(BaseModel):
    """Metadata tracking what data has been seeded."""

    symbol: str = Field(..., description="Stock ticker symbol")
    data_type: str = Field(..., description="Data type: 'historical' or 'intraday_{interval}'")
    first_date: datetime | None = Field(None, description="First date/time in the data")
    last_date: datetime | None = Field(None, description="Last date/time in the data")
    row_count: int | None = Field(None, description="Number of rows")
    last_seeded: datetime | None = Field(None, description="When the data was last seeded")
