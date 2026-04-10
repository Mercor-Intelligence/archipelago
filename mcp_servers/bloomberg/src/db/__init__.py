"""Database layer for offline mode with DuckDB."""

# Database core
# High-level database facade
# SQLAlchemy ORM models
from db.models.company_profile import CompanyProfile
from db.models.historical_price import HistoricalPrice
from db.models.intraday_bar import (
    INTERVAL_MAP,
    IntradayBar,
    IntradayBar1Hour,
    IntradayBar1Min,
    IntradayBar4Hour,
    IntradayBar5Min,
    IntradayBar15Min,
    IntradayBar30Min,
    get_intraday_model,
)
from db.models.seed_metadata import SeedMetadata

from .database import OfflineDatabase

# Pydantic schemas (for API responses)
from .schemas import HistoricalPriceSchema, IntradayBarSchema, SeedMetadataSchema

# Service layer
from .service import DuckDBService
from .session import INTRADAY_INTERVALS, Base, DatabaseSession, create_session, get_engine

__all__ = [
    # Core
    "Base",
    "DatabaseSession",
    "create_session",
    "get_engine",
    "INTRADAY_INTERVALS",
    # Facade
    "OfflineDatabase",
    # ORM models
    "CompanyProfile",
    "HistoricalPrice",
    "INTERVAL_MAP",
    "IntradayBar",
    "IntradayBar1Min",
    "IntradayBar5Min",
    "IntradayBar15Min",
    "IntradayBar30Min",
    "IntradayBar1Hour",
    "IntradayBar4Hour",
    "SeedMetadata",
    "get_intraday_model",
    # Pydantic schemas
    "HistoricalPriceSchema",
    "IntradayBarSchema",
    "SeedMetadataSchema",
    # Service
    "DuckDBService",
]
