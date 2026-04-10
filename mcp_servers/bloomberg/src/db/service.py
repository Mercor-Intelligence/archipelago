"""DuckDB service for offline data access.

This service provides read-only access to the seeded DuckDB database.
Uses SQLAlchemy ORM for Active Record-style queries.

Usage:
    # Service API
    with DuckDBService() as service:
        df = service.get_historical("AAPL", start_date, end_date)

    # Direct ORM (Active Record style)
    with DatabaseSession() as session:
        prices = HistoricalPrice.find_by_symbol(session, "AAPL")
        latest = HistoricalPrice.latest(session, "AAPL")
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from db.models.historical_price import HistoricalPrice
from db.models.intraday_bar import get_intraday_model
from db.models.seed_metadata import SeedMetadata
from db.schemas import HistoricalPriceSchema, IntradayBarSchema, SeedMetadataSchema
from db.session import DEFAULT_DB_PATH, INTRADAY_INTERVALS, DatabaseSession, get_engine


class DuckDBService:
    """Service for querying offline data from DuckDB.

    Provides read-only access to historical and intraday price data.
    Uses SQLAlchemy ORM internally for clean Active Record-style queries.

    Usage:
        service = DuckDBService()
        df = service.get_historical("AAPL", start_date, end_date)
        bars = service.get_intraday_bars("AAPL", "5min", start, end)

        # Or use direct ORM access:
        with service.session_scope() as session:
            prices = HistoricalPrice.find_by_symbol(session, "AAPL")
    """

    def __init__(self, db_path: Path | str | None = None):
        """Initialize the service.

        Args:
            db_path: Path to DuckDB database file. Defaults to data/offline.duckdb
        """
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._engine: Engine | None = None
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        """Get or create database session."""
        if self._session is None:
            if not self.db_path.exists():
                raise FileNotFoundError(f"Database not found: {self.db_path}")
            self._engine = get_engine(self.db_path, read_only=True)
            session_factory = sessionmaker(bind=self._engine)
            self._session = session_factory()
        return self._session

    def session_scope(self) -> DatabaseSession:
        """Get a session context manager for direct ORM access.

        Example:
            with service.session_scope() as session:
                prices = HistoricalPrice.find_by_symbol(session, "AAPL")
        """
        return DatabaseSession(self.db_path, read_only=True)

    def close(self) -> None:
        """Close the database session and dispose the engine."""
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def __enter__(self) -> "DuckDBService":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # -------------------------------------------------------------------------
    # Query Methods (delegate to ORM models)
    # -------------------------------------------------------------------------

    def get_symbols(self) -> list[str]:
        """Get list of all available symbols."""
        symbols = set(HistoricalPrice.symbols(self.session))
        for interval in INTRADAY_INTERVALS:
            try:
                model = get_intraday_model(interval)
                symbols.update(model.symbols(self.session))
            except Exception:
                pass
        return sorted(symbols)

    def get_historical(
        self,
        symbol: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> pd.DataFrame:
        """Get historical daily price data as DataFrame."""
        return HistoricalPrice.to_dataframe(self.session, symbol, start_date, end_date)

    def get_historical_as_models(
        self,
        symbol: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[HistoricalPriceSchema]:
        """Get historical data as Pydantic models."""
        records = HistoricalPrice.find_by_symbol(self.session, symbol, start_date, end_date)
        return [
            HistoricalPriceSchema(
                symbol=r.symbol,
                date=r.date,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                adj_close=r.adj_close,
                volume=r.volume,
            )
            for r in records
        ]

    def get_intraday_bars(
        self,
        symbol: str,
        interval: str = "5min",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Get intraday bar data as DataFrame."""
        model = get_intraday_model(interval)
        return model.to_dataframe(self.session, symbol, start, end)

    def get_intraday_bars_as_models(
        self,
        symbol: str,
        interval: str = "5min",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[IntradayBarSchema]:
        """Get intraday bars as Pydantic models."""
        model = get_intraday_model(interval)
        records = model.find_by_symbol(self.session, symbol, start, end)
        return [
            IntradayBarSchema(
                symbol=r.symbol,
                timestamp=r.timestamp,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
            )
            for r in records
        ]

    def get_latest_price(self, symbol: str) -> dict | None:
        """Get the latest available price for a symbol."""
        # Try historical first
        latest = HistoricalPrice.latest(self.session, symbol)
        if latest:
            return {
                **latest.to_dict(),
                "source": "historical",
            }

        # Try intraday intervals
        for interval in INTRADAY_INTERVALS:
            try:
                model = get_intraday_model(interval)
                bar = model.latest(self.session, symbol)
                if bar:
                    return {
                        **bar.to_dict(),
                        "source": f"intraday_{interval}",
                    }
            except Exception:
                continue

        return None

    def get_metadata(self, symbol: str | None = None) -> list[SeedMetadataSchema]:
        """Get seed metadata for symbols."""
        records = SeedMetadata.find_all(self.session, symbol)
        return [
            SeedMetadataSchema(
                symbol=r.symbol,
                data_type=r.data_type,
                first_date=r.first_date,
                last_date=r.last_date,
                row_count=r.row_count,
                last_seeded=r.last_seeded,
            )
            for r in records
        ]

    def get_data_ranges(self) -> dict[str, dict]:
        """Get available date ranges for all symbols."""
        ranges: dict[str, dict] = {}
        for meta in self.get_metadata():
            if meta.symbol not in ranges:
                ranges[meta.symbol] = {}
            ranges[meta.symbol][meta.data_type] = {
                "start": meta.first_date,
                "end": meta.last_date,
                "rows": meta.row_count,
            }
        return ranges

    def is_available(self, symbol: str) -> bool:
        """Check if data is available for a symbol."""
        return symbol.upper() in self.get_symbols()
