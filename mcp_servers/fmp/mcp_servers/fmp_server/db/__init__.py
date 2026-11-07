"""Database module for FMP MCP Server offline mode.

This module provides:
- SQLAlchemy async engine and session management
- ORM models for offline fixture storage
- Async session factory for database operations
"""

from .models import (
    Base,
    Company,
    FinancialStatement,
    GenericApiResponse,
    HistoricalPrice,
    StockQuote,
    TechnicalIndicator,
)
from .session import (
    close_engine,
    get_engine,
    get_session,
    init_db,
)

__all__ = [
    # Models
    "Base",
    "Company",
    "StockQuote",
    "HistoricalPrice",
    "FinancialStatement",
    "TechnicalIndicator",
    "GenericApiResponse",
    # Session management
    "get_engine",
    "get_session",
    "init_db",
    "close_engine",
]
