"""Company model for storing company profiles and metadata."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Company(Base):
    """Company profiles and metadata.

    Stores comprehensive company information including profile data,
    sector/industry classification, and other metadata from FMP API.

    Attributes:
        symbol: Stock ticker symbol (primary key)
        company_name: Full company name
        exchange: Stock exchange where listed
        exchange_short_name: Short form of exchange name
        currency: Trading currency
        country: Country of incorporation
        sector: Business sector classification
        industry: Industry classification within sector
        cik: SEC CIK number
        cusip: CUSIP identifier
        isin: ISIN identifier
        description: Company business description
        website: Company website URL
        logo: Company logo URL
        ceo: CEO name
        full_time_employees: Number of full-time employees
        ipo_date: IPO date
        address: Company headquarters address
        city: Headquarters city
        state: Headquarters state
        zip: Headquarters ZIP code
        phone: Company phone number
        is_etf: Whether this is an ETF
        is_actively_trading: Whether actively trading
        profile_data: Full JSON profile data from API
        created_at: Record creation timestamp
        updated_at: Record last update timestamp
    """

    __tablename__ = "companies"

    # Primary key
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)

    # Basic info
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(50), nullable=True)
    exchange_short_name: Mapped[str | None] = mapped_column(String(20), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Classification (indexed for filtering via __table_args__)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Identifiers
    cik: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cusip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Profile details
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    logo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ceo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_time_employees: Mapped[int | None] = mapped_column(nullable=True)
    ipo_date: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Address
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Flags
    is_etf: Mapped[bool | None] = mapped_column(nullable=True, default=False)
    is_actively_trading: Mapped[bool | None] = mapped_column(nullable=True, default=True)

    # Full JSON data for any additional fields
    profile_data: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # Indexes defined via __table_args__
    __table_args__ = (
        Index("ix_companies_sector", "sector"),
        Index("ix_companies_industry", "industry"),
    )

    def __repr__(self) -> str:
        return f"<Company(symbol={self.symbol!r}, name={self.company_name!r})>"
