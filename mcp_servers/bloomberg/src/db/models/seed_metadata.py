"""Seed metadata ORM model."""

from datetime import datetime
from typing import Self

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from db.session import Base


class SeedMetadata(Base):
    """Metadata tracking what data has been seeded."""

    __tablename__ = "seed_metadata"

    # Composite primary key: (symbol, data_type)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    data_type: Mapped[str] = mapped_column(String, primary_key=True)
    first_date: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_date: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=True)
    last_seeded: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    @classmethod
    def query(cls, session: Session):
        return session.query(cls)

    @classmethod
    def find_all(cls, session: Session, symbol: str | None = None) -> list[Self]:
        """Find all metadata, optionally filtered by symbol."""
        stmt = select(cls)
        if symbol:
            stmt = stmt.where(cls.symbol == symbol.upper())
        stmt = stmt.order_by(cls.symbol)
        return list(session.scalars(stmt).all())

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "data_type": self.data_type,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "row_count": self.row_count,
            "last_seeded": self.last_seeded,
        }
