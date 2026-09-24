"""SQLAlchemy ORM models for FMP offline fixture storage.

This module defines the database schema for storing FMP API responses
for offline mode operation. The schema supports:
- Company profiles and metadata
- Stock quotes (latest snapshots)
- Historical price data (OHLCV)
- Financial statements (income, balance sheet, cash flow)
- Technical indicators (SMA, EMA, RSI, etc.)
- Generic API responses (catch-all for other endpoints)
"""

from .base import Base
from .company import Company
from .financial_statement import FISCAL_DATE_SENTINEL, FinancialStatement
from .generic_api_response import GenericApiResponse
from .historical_price import HistoricalPrice
from .stock_quote import StockQuote
from .technical_indicator import TechnicalIndicator

__all__ = [
    "Base",
    "Company",
    "StockQuote",
    "HistoricalPrice",
    "FinancialStatement",
    "FISCAL_DATE_SENTINEL",
    "TechnicalIndicator",
    "GenericApiResponse",
]
