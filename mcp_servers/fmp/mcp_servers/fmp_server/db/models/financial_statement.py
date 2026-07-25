"""Financial statement model for storing income, balance sheet, and cash flow data."""

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# Sentinel date used for records without a fiscal date (e.g., TTM metrics)
# This ensures the unique constraint works correctly since SQL treats NULLs as distinct
FISCAL_DATE_SENTINEL = date(1, 1, 1)


class FinancialStatement(Base):
    """Financial statement data (income, balance sheet, cash flow).

    Stores financial statement data for each symbol including income statements,
    balance sheets, and cash flow statements from FMP API.

    The statement_type field indicates which type of statement:
    - 'income': Income statement
    - 'balance_sheet': Balance sheet
    - 'cash_flow': Cash flow statement
    - 'income_ttm': Trailing twelve months income
    - 'balance_sheet_ttm': Trailing twelve months balance sheet
    - 'cash_flow_ttm': Trailing twelve months cash flow
    - 'key_metrics': Key financial metrics
    - 'key_metrics_ttm': TTM key metrics
    - 'ratios': Financial ratios
    - 'ratios_ttm': TTM ratios
    - 'growth': Financial growth metrics
    - 'income_growth': Income statement growth
    - 'balance_sheet_growth': Balance sheet growth
    - 'cash_flow_growth': Cash flow growth
    - 'scores': Financial health scores

    Attributes:
        id: Auto-increment primary key
        symbol: Stock ticker symbol
        statement_type: Type of financial statement
        period: Reporting period ('annual' or 'quarter')
        fiscal_date: Fiscal date of the statement (uses FISCAL_DATE_SENTINEL for TTM)
        fiscal_year: Fiscal year
        calendar_year: Calendar year
        reported_currency: Currency of reported values
        filing_date: SEC filing date
        accepted_date: SEC accepted date
        cik: SEC CIK number
        statement_data: Full JSON statement data from API
        created_at: Record creation timestamp
        updated_at: Record last update timestamp
    """

    __tablename__ = "financial_statements"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Symbol (indexed for lookup via __table_args__)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)

    # Statement classification (unique together with symbol, period, fiscal_date)
    statement_type: Mapped[str] = mapped_column(String(50), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)  # 'annual' or 'quarter'

    # Dates
    fiscal_date: Mapped[date] = mapped_column(Date, nullable=False, default=FISCAL_DATE_SENTINEL)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calendar_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Metadata
    reported_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    filing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    accepted_date: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cik: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Full JSON data containing all financial statement fields
    statement_data: Mapped[str] = mapped_column(Text, nullable=False)

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
            "statement_type",
            "period",
            "fiscal_date",
            name="uq_financial_statements_symbol_type_period_date",
        ),
        Index("ix_financial_statements_symbol", "symbol"),
        Index("ix_financial_statements_statement_type", "statement_type"),
        Index(
            "ix_financial_statements_symbol_type",
            "symbol",
            "statement_type",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FinancialStatement(symbol={self.symbol!r}, "
            f"type={self.statement_type!r}, period={self.period!r})>"
        )
