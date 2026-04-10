"""Historical price ORM model."""

from datetime import date as DateType
from datetime import datetime
from typing import Self

import pandas as pd
from sqlalchemy import Date, Float, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from db.session import Base


class HistoricalPrice(Base):
    """Historical daily OHLCV price data."""

    __tablename__ = "historical_prices"

    # Composite primary key: (symbol, date)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[DateType] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float, nullable=True)
    high: Mapped[float] = mapped_column(Float, nullable=True)
    low: Mapped[float] = mapped_column(Float, nullable=True)
    close: Mapped[float] = mapped_column(Float, nullable=True)
    adj_close: Mapped[float] = mapped_column(Float, nullable=True)
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
        start_date: datetime | DateType | None = None,
        end_date: datetime | DateType | None = None,
    ) -> list[Self]:
        """Find all records for a symbol within date range."""
        stmt = select(cls).where(cls.symbol == symbol.upper())

        if start_date:
            date_val = start_date.date() if isinstance(start_date, datetime) else start_date
            stmt = stmt.where(cls.date >= date_val)
        if end_date:
            date_val = end_date.date() if isinstance(end_date, datetime) else end_date
            stmt = stmt.where(cls.date <= date_val)

        stmt = stmt.order_by(cls.date)
        return list(session.scalars(stmt).all())

    @classmethod
    def latest(cls, session: Session, symbol: str) -> Self | None:
        """Find the latest record for a symbol."""
        stmt = select(cls).where(cls.symbol == symbol.upper()).order_by(cls.date.desc()).limit(1)
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
        start_date: datetime | DateType | None = None,
        end_date: datetime | DateType | None = None,
    ) -> pd.DataFrame:
        """Get records as DataFrame (FMP-compatible format)."""
        records = cls.find_by_symbol(session, symbol, start_date, end_date)
        if not records:
            return pd.DataFrame()

        data = [
            {
                "date": r.date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "adjclose": r.adj_close,  # FMP format
                "volume": r.volume,
            }
            for r in records
        ]
        return pd.DataFrame(data)

    @classmethod
    def stats(cls, session: Session) -> list[dict]:
        """Get statistics grouped by symbol."""
        stmt = (
            select(
                cls.symbol,
                func.count().label("rows"),
                func.min(cls.date).label("first_date"),
                func.max(cls.date).label("last_date"),
                func.round(func.min(cls.close), 2).label("min_price"),
                func.round(func.max(cls.close), 2).label("max_price"),
            )
            .group_by(cls.symbol)
            .order_by(cls.symbol)
        )

        result = session.execute(stmt).all()
        return [
            {
                "symbol": row.symbol,
                "rows": row.rows,
                "first_date": row.first_date,
                "last_date": row.last_date,
                "min_price": row.min_price,
                "max_price": row.max_price,
            }
            for row in result
        ]

    @classmethod
    def insert_many(cls, session: Session, symbol: str, rows: list[dict]) -> int:
        """Insert multiple rows. Returns count of inserted rows.

        Args:
            session: Database session
            symbol: Stock symbol
            rows: List of dicts with keys: date, open, high, low, close, adjClose, volume

        Returns:
            Number of rows inserted
        """
        inserted = 0
        for row in rows:
            obj = cls(
                symbol=symbol.upper(),
                date=row["date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                adj_close=row["adjClose"],
                volume=row["volume"],
            )
            session.merge(obj)
            inserted += 1

        session.commit()
        return inserted

    @classmethod
    def delete_symbol(cls, session: Session, symbol: str) -> int:
        """Delete all data for a symbol. Returns count of deleted rows."""
        count = session.query(cls).filter(cls.symbol == symbol.upper()).delete()
        session.commit()
        return count

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "symbol": self.symbol,
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "adj_close": self.adj_close,
            "volume": self.volume,
        }
