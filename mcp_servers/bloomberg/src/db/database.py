"""High-level database facade for offline mode."""

from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from db.models.company_profile import CompanyProfile
from db.models.historical_price import HistoricalPrice
from db.models.intraday_bar import get_intraday_model
from db.models.seed_metadata import SeedMetadata
from db.session import DEFAULT_DB_PATH, INTRADAY_INTERVALS, Base, get_engine


class OfflineDatabase:
    """Manages the offline DuckDB database using SQLAlchemy ORM."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._engine = None
        self._session: Session | None = None

    @property
    def engine(self):
        """Get or create SQLAlchemy engine."""
        if self._engine is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._engine = get_engine(self.db_path, read_only=False)
        return self._engine

    @property
    def session(self) -> Session:
        """Get or create database session."""
        if self._session is None:
            session_factory = sessionmaker(bind=self.engine)
            self._session = session_factory()
        return self._session

    def close(self) -> None:
        """Close the database connection."""
        if self._session is not None:
            # Checkpoint WAL to ensure all data is flushed to main database file
            # This is critical for read-only connections to see the data
            try:
                self._session.execute(text("CHECKPOINT"))
                self._session.commit()
            except Exception:
                pass  # Checkpoint might fail if no WAL exists
            self._session.close()
            self._session = None
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def __enter__(self) -> "OfflineDatabase":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def create_schema(self) -> None:
        """Create all tables from SQLAlchemy ORM models."""
        Base.metadata.create_all(self.engine)

    def insert_historical(self, symbol: str, rows: list[dict]) -> int:
        """Insert historical price rows using ORM. Returns count of inserted rows."""
        symbol = symbol.upper()
        inserted = 0
        for row in rows:
            obj = HistoricalPrice(
                symbol=symbol,
                date=row["date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                adj_close=row["adjClose"],
                volume=row["volume"],
            )
            self.session.merge(obj)
            inserted += 1

        self.session.commit()
        return inserted

    def insert_intraday(self, symbol: str, interval: str, rows: list[dict]) -> int:
        """Insert intraday bar rows using ORM. Returns count of inserted rows."""
        symbol = symbol.upper()
        Model = get_intraday_model(interval)
        inserted = 0

        for row in rows:
            obj = Model(
                symbol=symbol,
                timestamp=row["date"],  # FMP uses 'date' for timestamp
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
            )
            self.session.merge(obj)
            inserted += 1

        self.session.commit()
        return inserted

    def get_tables(self) -> list[str]:
        """Get list of all tables in the database."""
        from sqlalchemy import inspect

        inspector = inspect(self.engine)
        return inspector.get_table_names()

    def get_row_count(self, table: str) -> int:
        """Get row count for a table."""
        # Validate table name against actual tables to prevent SQL injection
        valid_tables = self.get_tables()
        if table not in valid_tables:
            raise ValueError(f"Invalid table name: {table}")
        result = self.session.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return result.scalar() or 0

    def get_historical_stats(self) -> list[dict]:
        """Get statistics for historical_prices table."""
        stmt = (
            select(
                HistoricalPrice.symbol,
                func.count().label("rows"),
                func.min(HistoricalPrice.date).label("first_date"),
                func.max(HistoricalPrice.date).label("last_date"),
                func.round(func.min(HistoricalPrice.close), 2).label("min_price"),
                func.round(func.max(HistoricalPrice.close), 2).label("max_price"),
            )
            .group_by(HistoricalPrice.symbol)
            .order_by(HistoricalPrice.symbol)
        )

        result = self.session.execute(stmt).all()
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

    def get_intraday_stats(self, interval: str) -> list[dict]:
        """Get statistics for an intraday table."""
        try:
            Model = get_intraday_model(interval)
        except ValueError:
            return []

        stmt = (
            select(
                Model.symbol,
                func.count().label("rows"),
                func.min(Model.timestamp).label("first_ts"),
                func.max(Model.timestamp).label("last_ts"),
            )
            .group_by(Model.symbol)
            .order_by(Model.symbol)
        )

        try:
            result = self.session.execute(stmt).all()
            return [
                {
                    "symbol": row.symbol,
                    "rows": row.rows,
                    "first_ts": row.first_ts,
                    "last_ts": row.last_ts,
                }
                for row in result
            ]
        except Exception:
            return []

    def delete_symbol(self, symbol: str) -> dict[str, int]:
        """Delete all data for a symbol from all tables."""
        symbol = symbol.upper()
        deleted = {}

        # Delete from historical_prices
        count = (
            self.session.query(HistoricalPrice).filter(HistoricalPrice.symbol == symbol).delete()
        )
        if count > 0:
            deleted["historical_prices"] = count

        # Delete from intraday tables
        for interval in INTRADAY_INTERVALS:
            try:
                Model = get_intraday_model(interval)
                count = self.session.query(Model).filter(Model.symbol == symbol).delete()
                if count > 0:
                    deleted[f"intraday_bars_{interval}"] = count
            except Exception:
                pass

        # Delete from company_profiles
        count = self.session.query(CompanyProfile).filter(CompanyProfile.symbol == symbol).delete()
        if count > 0:
            deleted["company_profiles"] = count

        # Delete seed_metadata for this symbol
        count = self.session.query(SeedMetadata).filter(SeedMetadata.symbol == symbol).delete()
        if count > 0:
            deleted["seed_metadata"] = count

        self.session.commit()
        return deleted

    def get_symbols(self) -> list[str]:
        """Get list of all symbols in the database."""
        symbols = set()

        # From historical
        try:
            result = self.session.execute(select(HistoricalPrice.symbol).distinct()).scalars()
            symbols.update(result)
        except Exception:
            pass

        # From intraday tables
        for interval in INTRADAY_INTERVALS:
            try:
                Model = get_intraday_model(interval)
                result = self.session.execute(select(Model.symbol).distinct()).scalars()
                symbols.update(result)
            except Exception:
                pass

        return sorted(symbols)

    # -------------------------------------------------------------------------
    # Metadata methods
    # -------------------------------------------------------------------------

    def get_metadata(self, symbol: str, data_type: str) -> dict | None:
        """Get metadata for a symbol/data_type combination."""
        result = self.session.execute(
            select(SeedMetadata).where(
                SeedMetadata.symbol == symbol.upper(), SeedMetadata.data_type == data_type
            )
        ).scalar_one_or_none()

        if result is None:
            return None

        return {
            "first_date": result.first_date,
            "last_date": result.last_date,
            "row_count": result.row_count,
            "last_seeded": result.last_seeded,
        }

    def update_metadata(
        self,
        symbol: str,
        data_type: str,
        first_date: datetime,
        last_date: datetime,
        row_count: int,
    ) -> None:
        """Update metadata after seeding."""
        obj = SeedMetadata(
            symbol=symbol.upper(),
            data_type=data_type,
            first_date=first_date,
            last_date=last_date,
            row_count=row_count,
            last_seeded=datetime.now(),
        )
        self.session.merge(obj)
        self.session.commit()

    def needs_seeding(
        self,
        symbol: str,
        data_type: str,
    ) -> tuple[bool, str]:
        """Check if a symbol/data_type needs seeding."""
        metadata = self.get_metadata(symbol, data_type)

        if metadata is None:
            return True, "no existing data"

        row_count = metadata["row_count"]
        first_date = metadata["first_date"]
        last_date = metadata["last_date"]

        return False, f"exists ({row_count} rows, {first_date} to {last_date})"

    def get_all_metadata(self) -> list[dict]:
        """Get all metadata records."""
        stmt = select(SeedMetadata).order_by(SeedMetadata.symbol, SeedMetadata.data_type)
        result = self.session.execute(stmt).scalars()

        return [
            {
                "symbol": row.symbol,
                "data_type": row.data_type,
                "first_date": row.first_date,
                "last_date": row.last_date,
                "row_count": row.row_count,
                "last_seeded": row.last_seeded,
            }
            for row in result
        ]
