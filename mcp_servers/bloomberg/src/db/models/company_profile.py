"""Company profile ORM model."""

from datetime import datetime
from typing import Self

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from db.session import Base


class CompanyProfile(Base):
    """Company profile and metadata for screening."""

    __tablename__ = "company_profiles"

    # Primary key
    symbol: Mapped[str] = mapped_column(String, primary_key=True)

    # Company info
    company_name: Mapped[str] = mapped_column(String, nullable=True)
    sector: Mapped[str] = mapped_column(String, nullable=True)
    industry: Mapped[str] = mapped_column(String, nullable=True)
    exchange: Mapped[str] = mapped_column(String, nullable=True)
    exchange_short: Mapped[str] = mapped_column(String, nullable=True)
    country: Mapped[str] = mapped_column(String, nullable=True)

    # Market data
    market_cap: Mapped[int] = mapped_column(BigInteger, nullable=True)
    price: Mapped[float] = mapped_column(Float, nullable=True)
    beta: Mapped[float] = mapped_column(Float, nullable=True)
    vol_avg: Mapped[int] = mapped_column(BigInteger, nullable=True)
    last_div: Mapped[float] = mapped_column(Float, nullable=True)

    # Flags
    is_etf: Mapped[bool] = mapped_column(Boolean, nullable=True)
    is_actively_trading: Mapped[bool] = mapped_column(Boolean, nullable=True)

    # Metadata
    last_updated: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    @classmethod
    def find_by_symbol(cls, session: Session, symbol: str) -> Self | None:
        """Find profile by symbol."""
        stmt = select(cls).where(cls.symbol == symbol.upper())
        return session.scalars(stmt).first()

    @classmethod
    def symbols(cls, session: Session) -> list[str]:
        """Get all unique symbols."""
        stmt = select(cls.symbol).distinct()
        return list(session.scalars(stmt).all())

    @classmethod
    def find_all(cls, session: Session) -> list[Self]:
        """Get all profiles."""
        stmt = select(cls).order_by(cls.symbol)
        return list(session.scalars(stmt).all())

    @classmethod
    def screen(
        cls,
        session: Session,
        sector: str | None = None,
        market_cap_min: float | None = None,
        market_cap_max: float | None = None,
        country: str | None = None,
        is_etf: bool | None = None,
        is_actively_trading: bool | None = None,
        limit: int = 100,
    ) -> list[Self]:
        """Screen profiles by criteria."""
        stmt = select(cls)

        if sector:
            stmt = stmt.where(cls.sector == sector)
        if market_cap_min is not None:
            stmt = stmt.where(cls.market_cap >= int(market_cap_min))
        if market_cap_max is not None:
            stmt = stmt.where(cls.market_cap <= int(market_cap_max))
        if country:
            stmt = stmt.where(cls.country == country)
        if is_etf is not None:
            stmt = stmt.where(cls.is_etf == is_etf)
        if is_actively_trading is not None:
            stmt = stmt.where(cls.is_actively_trading == is_actively_trading)

        stmt = stmt.order_by(cls.market_cap.desc()).limit(limit)
        return list(session.scalars(stmt).all())

    @classmethod
    def upsert(cls, session: Session, data: dict) -> Self:
        """Insert or update a profile."""
        obj = cls(
            symbol=data["symbol"].upper(),
            company_name=data.get("companyName"),
            sector=data.get("sector"),
            industry=data.get("industry"),
            exchange=data.get("exchange"),
            exchange_short=data.get("exchangeShortName"),
            country=data.get("country"),
            market_cap=data.get("mktCap"),
            price=data.get("price"),
            beta=data.get("beta"),
            vol_avg=data.get("volAvg"),
            last_div=data.get("lastDiv"),
            is_etf=data.get("isEtf"),
            is_actively_trading=data.get("isActivelyTrading"),
            last_updated=datetime.now(),
        )
        session.merge(obj)
        return obj

    @classmethod
    def upsert_many(cls, session: Session, profiles: list[dict]) -> int:
        """Insert or update multiple profiles. Returns count."""
        count = 0
        for data in profiles:
            cls.upsert(session, data)
            count += 1
        session.commit()
        return count

    def to_dict(self) -> dict:
        """Convert to dictionary (FMP-compatible format)."""
        return {
            "symbol": self.symbol,
            "companyName": self.company_name,
            "sector": self.sector,
            "industry": self.industry,
            "exchange": self.exchange,
            "exchangeShortName": self.exchange_short,
            "country": self.country,
            "marketCap": self.market_cap,
            "price": self.price,
            "beta": self.beta,
            "volAvg": self.vol_avg,
            "lastAnnualDividend": self.last_div,
            "isEtf": self.is_etf,
            "isActivelyTrading": self.is_actively_trading,
        }
