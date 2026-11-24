"""Technical indicator model for storing SMA, EMA, RSI, and other indicators."""

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TechnicalIndicator(Base):
    """Technical indicator data (SMA, EMA, RSI, etc.).

    Stores technical indicator values for each symbol from FMP API.

    The indicator_type field indicates which indicator:
    - 'sma': Simple Moving Average
    - 'ema': Exponential Moving Average
    - 'wma': Weighted Moving Average
    - 'dema': Double Exponential Moving Average
    - 'tema': Triple Exponential Moving Average
    - 'williams': Williams %R
    - 'rsi': Relative Strength Index
    - 'adx': Average Directional Index
    - 'standard_deviation': Standard Deviation

    Attributes:
        id: Auto-increment primary key
        symbol: Stock ticker symbol
        indicator_type: Type of technical indicator
        period: Indicator calculation period (e.g., 14, 20, 50)
        interval: Data interval (e.g., '1day', '1hour', '15min')
        date: Date of the indicator value
        value: The calculated indicator value
        open: Opening price at this date
        high: High price at this date
        low: Low price at this date
        close: Closing price at this date
        volume: Volume at this date
        indicator_data: Full JSON indicator data from API
        created_at: Record creation timestamp
        updated_at: Record last update timestamp
    """

    __tablename__ = "technical_indicators"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Symbol (indexed for lookup via __table_args__)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)

    # Indicator classification (unique together with symbol, period, interval, date)
    indicator_type: Mapped[str] = mapped_column(String(30), nullable=False)
    period: Mapped[int] = mapped_column(Integer, nullable=False)  # e.g., 14, 20, 50
    interval: Mapped[str] = mapped_column(
        String(20), nullable=False, default="1day"
    )  # e.g., '1day', '1hour'

    # Date of indicator value
    date: Mapped[date] = mapped_column(Date, nullable=False)

    # Indicator value
    value: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Associated OHLCV data
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(nullable=True)

    # Full JSON data for any additional fields
    indicator_data: Mapped[str | None] = mapped_column(Text, nullable=True)

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
        UniqueConstraint(
            "symbol",
            "indicator_type",
            "period",
            "interval",
            "date",
            name="uq_technical_indicators_symbol_type_period_interval_date",
        ),
        Index("ix_technical_indicators_symbol", "symbol"),
        Index("ix_technical_indicators_type", "indicator_type"),
        Index(
            "ix_technical_indicators_symbol_type_period",
            "symbol",
            "indicator_type",
            "period",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<TechnicalIndicator(symbol={self.symbol!r}, "
            f"type={self.indicator_type!r}, period={self.period}, value={self.value})>"
        )
