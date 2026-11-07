"""Stock quote model for storing latest quote snapshots."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class StockQuote(Base):
    """Latest stock quote snapshots.

    Stores the most recent quote data for each symbol including price,
    volume, and market data from FMP API.

    Attributes:
        symbol: Stock ticker symbol (primary key)
        price: Current stock price
        open: Opening price
        high: Day's high price
        low: Day's low price
        previous_close: Previous day's closing price
        change: Price change from previous close
        change_percent: Percentage change from previous close
        volume: Trading volume
        avg_volume: Average trading volume
        market_cap: Market capitalization
        pe_ratio: Price-to-earnings ratio
        eps: Earnings per share
        fifty_two_week_high: 52-week high price
        fifty_two_week_low: 52-week low price
        shares_outstanding: Shares outstanding
        quote_data: Full JSON quote data from API
        quote_timestamp: Timestamp of the quote from API
        created_at: Record creation timestamp
        updated_at: Record last update timestamp
    """

    __tablename__ = "stock_quotes"

    # Primary key
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)

    # Price data
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_close: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Change data
    change: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Volume data
    volume: Mapped[int | None] = mapped_column(nullable=True)
    avg_volume: Mapped[int | None] = mapped_column(nullable=True)

    # Market data
    market_cap: Mapped[int | None] = mapped_column(nullable=True)
    pe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 52-week data
    fifty_two_week_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    fifty_two_week_low: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Shares data
    shares_outstanding: Mapped[int | None] = mapped_column(nullable=True)

    # Full JSON data for any additional fields
    quote_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Quote timestamp from API
    quote_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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

    def __repr__(self) -> str:
        return f"<StockQuote(symbol={self.symbol!r}, price={self.price})>"
