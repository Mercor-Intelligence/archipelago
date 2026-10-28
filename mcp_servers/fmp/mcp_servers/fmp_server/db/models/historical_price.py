"""Historical price model for storing daily OHLCV data."""

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class HistoricalPrice(Base):
    """Daily OHLCV historical price data.

    Stores daily price data for each symbol including open, high, low,
    close, volume, and adjusted values from FMP API.

    Price Adjustment Types:
        - open/high/low/close: Split-adjusted prices (adjusted for stock splits only)
        - adj_open/adj_high/adj_low/adj_close: Fully adjusted prices (splits + dividends)

    For total return calculations, use adj_close. For charting price history
    without dividend effects, use close.

    Attributes:
        id: Auto-increment primary key
        symbol: Stock ticker symbol
        date: Trading date
        open: Split-adjusted opening price (adjusted for stock splits only)
        high: Split-adjusted day's high price (adjusted for stock splits only)
        low: Split-adjusted day's low price (adjusted for stock splits only)
        close: Split-adjusted closing price (adjusted for stock splits only)
        adj_open: Fully adjusted opening price (adjusted for splits AND dividends)
        adj_high: Fully adjusted high price (adjusted for splits AND dividends)
        adj_low: Fully adjusted low price (adjusted for splits AND dividends)
        adj_close: Fully adjusted closing price (adjusted for splits AND dividends)
        volume: Trading volume
        unadjusted_volume: Unadjusted trading volume
        change: Price change
        change_percent: Percentage change
        vwap: Volume-weighted average price
        label: Human-readable date label
        change_over_time: Cumulative change over time
        price_data: Full JSON price data from API
        created_at: Record creation timestamp
        updated_at: Record last update timestamp
    """

    __tablename__ = "historical_prices"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Symbol and date (unique together, indexed via __table_args__)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)

    # Split-adjusted OHLCV data (adjusted for stock splits only)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(nullable=True)
    unadjusted_volume: Mapped[int | None] = mapped_column(nullable=True)

    # Fully adjusted OHLC prices (adjusted for splits AND dividends)
    adj_open: Mapped[float | None] = mapped_column(Float, nullable=True)
    adj_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    adj_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    adj_close: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Change data
    change: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    vwap: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Additional fields
    label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    change_over_time: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Full JSON data for any additional fields
    price_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_historical_prices_symbol_date"),
        Index("ix_historical_prices_symbol", "symbol"),
        Index("ix_historical_prices_date", "date"),
        Index("ix_historical_prices_symbol_date", "symbol", "date"),
    )

    def __repr__(self) -> str:
        return f"<HistoricalPrice(symbol={self.symbol!r}, date={self.date}, close={self.close})>"
