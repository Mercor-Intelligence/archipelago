"""Intraday bar ORM models."""

from datetime import datetime
from typing import Self

import pandas as pd
from sqlalchemy import DateTime, Float, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from db.session import Base

INTERVAL_MAP = {
    "1m": "1min",
    "1min": "1min",
    "5m": "5min",
    "5min": "5min",
    "15m": "15min",
    "15min": "15min",
    "30m": "30min",
    "30min": "30min",
    "1h": "1hour",
    "1hour": "1hour",
    "60m": "1hour",
    "4h": "4hour",
    "4hour": "4hour",
}


class IntradayBar(Base):
    """Intraday OHLCV bar data - base class for interval-specific tables."""

    __abstract__ = True

    # Composite primary key: (symbol, timestamp)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[float] = mapped_column(Float, nullable=True)
    high: Mapped[float] = mapped_column(Float, nullable=True)
    low: Mapped[float] = mapped_column(Float, nullable=True)
    close: Mapped[float] = mapped_column(Float, nullable=True)
    volume: Mapped[int] = mapped_column(Integer, nullable=True)

    @classmethod
    def query(cls, session: Session):
        """Start a query for this model."""
        return session.query(cls)

    @classmethod
    def find_by_symbol(
        cls,
        session: Session,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Self]:
        """Find all bars for a symbol within time range."""
        stmt = select(cls).where(cls.symbol == symbol.upper())

        if start:
            stmt = stmt.where(cls.timestamp >= start)
        if end:
            stmt = stmt.where(cls.timestamp <= end)

        stmt = stmt.order_by(cls.timestamp)
        return list(session.scalars(stmt).all())

    @classmethod
    def latest(cls, session: Session, symbol: str) -> Self | None:
        """Find the latest bar for a symbol."""
        stmt = (
            select(cls).where(cls.symbol == symbol.upper()).order_by(cls.timestamp.desc()).limit(1)
        )
        return session.scalars(stmt).first()

    @classmethod
    def symbols(cls, session: Session) -> list[str]:
        """Get all unique symbols."""
        stmt = select(cls.symbol).distinct()
        return list(session.scalars(stmt).all())

    @classmethod
    def to_dataframe(
        cls,
        session: Session,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Get bars as DataFrame (FMP-compatible format with date index)."""
        records = cls.find_by_symbol(session, symbol, start, end)
        if not records:
            return pd.DataFrame()

        data = [
            {
                "date": r.timestamp,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
            }
            for r in records
        ]
        df = pd.DataFrame(data)
        df = df.set_index("date")
        return df

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


# Interval-specific tables
class IntradayBar1Min(IntradayBar):
    __tablename__ = "intraday_bars_1min"


class IntradayBar5Min(IntradayBar):
    __tablename__ = "intraday_bars_5min"


class IntradayBar15Min(IntradayBar):
    __tablename__ = "intraday_bars_15min"


class IntradayBar30Min(IntradayBar):
    __tablename__ = "intraday_bars_30min"


class IntradayBar1Hour(IntradayBar):
    __tablename__ = "intraday_bars_1hour"


class IntradayBar4Hour(IntradayBar):
    __tablename__ = "intraday_bars_4hour"


# Mapping from normalized interval suffix to model class
INTRADAY_MODELS: dict[str, type[IntradayBar]] = {
    "1min": IntradayBar1Min,
    "5min": IntradayBar5Min,
    "15min": IntradayBar15Min,
    "30min": IntradayBar30Min,
    "1hour": IntradayBar1Hour,
    "4hour": IntradayBar4Hour,
}


def get_intraday_model(interval: str) -> type[IntradayBar]:
    """Get the model class for an interval."""
    suffix = INTERVAL_MAP.get(interval.lower())
    if not suffix:
        raise ValueError(f"Unknown interval: {interval}. Valid: {list(INTERVAL_MAP.keys())}")
    return INTRADAY_MODELS[suffix]
