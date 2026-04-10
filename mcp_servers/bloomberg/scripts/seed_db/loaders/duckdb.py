"""DuckDB loader for inserting data into the offline database."""

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select

from db import INTERVAL_MAP, CompanyProfile, HistoricalPrice, OfflineDatabase, get_intraday_model

from .base import BaseLoader


class DuckDBLoader(BaseLoader):
    """Loader that inserts data into DuckDB via OfflineDatabase."""

    def __init__(self, db_path: str | Path | None = None):
        """Initialize the loader.

        Args:
            db_path: Path to DuckDB file. Uses default if None.
        """
        self._db = OfflineDatabase(db_path)
        self._db.create_schema()

    @property
    def db(self) -> OfflineDatabase:
        """Access to underlying database for advanced operations."""
        return self._db

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()

    def load_historical(self, symbol: str, raw_data: dict) -> int:
        """Load historical data from raw format into DuckDB.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            raw_data: Raw data dict with 'data.historical' list

        Returns:
            Number of rows inserted
        """
        data = raw_data.get("data", {})
        rows = data.get("historical", [])

        if not rows:
            return 0

        inserted = self._db.insert_historical(symbol, rows)

        # Update metadata
        dates = [row["date"] for row in rows]
        self._db.update_metadata(
            symbol=symbol,
            data_type="historical",
            first_date=datetime.fromisoformat(min(dates)),
            last_date=datetime.fromisoformat(max(dates)),
            row_count=inserted,
        )

        return inserted

    def load_intraday(self, symbol: str, interval: str, raw_data: dict) -> int:
        """Load intraday data from raw format into DuckDB.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            interval: Bar interval (e.g., "5min", "15min", "1hour")
            raw_data: Raw data dict with 'data.bars' list

        Returns:
            Number of rows inserted
        """
        data = raw_data.get("data", {})
        rows = data.get("bars", [])

        if not rows:
            return 0

        inserted = self._db.insert_intraday(symbol, interval, rows)

        # Update metadata
        normalized_interval = INTERVAL_MAP.get(interval.lower(), interval)
        data_type = f"intraday_{normalized_interval}"
        timestamps = [row["date"] for row in rows]
        self._db.update_metadata(
            symbol=symbol,
            data_type=data_type,
            first_date=datetime.fromisoformat(min(timestamps)),
            last_date=datetime.fromisoformat(max(timestamps)),
            row_count=inserted,
        )

        return inserted

    def load_profile(self, symbol: str, raw_data: dict) -> int:
        """Load company profile from raw format into DuckDB.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            raw_data: Raw data dict with 'data.profile' dict

        Returns:
            Number of rows inserted (0 or 1)
        """
        data = raw_data.get("data", {})
        profile = data.get("profile")

        if not profile:
            return 0

        # Use upsert_many with a single-item list
        count = CompanyProfile.upsert_many(self._db.session, [profile])
        return count

    def load_profiles_batch(self, raw_data: dict) -> int:
        """Load multiple company profiles from raw format into DuckDB.

        Args:
            raw_data: Raw data dict with 'data.profiles' list

        Returns:
            Number of profiles inserted
        """
        data = raw_data.get("data", {})
        profiles = data.get("profiles", [])

        if not profiles:
            return 0

        count = CompanyProfile.upsert_many(self._db.session, profiles)

        # Update metadata with total count
        total_profiles = len(CompanyProfile.symbols(self._db.session))
        self._db.update_metadata(
            symbol="__ALL__",
            data_type="profiles",
            first_date=datetime.now(),
            last_date=datetime.now(),
            row_count=total_profiles,
        )

        return count

    def needs_loading(self, symbol: str, data_type: str) -> tuple[bool, str]:
        """Check if data needs to be loaded for a symbol.

        Args:
            symbol: Stock symbol
            data_type: Type of data ('historical', 'intraday_5min', etc.)

        Returns:
            Tuple of (needs_loading, reason_string)
        """
        return self._db.needs_seeding(symbol, data_type)

    def get_profile_symbols(self) -> set[str]:
        """Get set of symbols that have profiles loaded."""
        return set(CompanyProfile.symbols(self._db.session))

    def get_last_historical_date(self, symbol: str) -> date | None:
        """Get the most recent date for historical data.

        Args:
            symbol: Stock symbol

        Returns:
            Most recent date or None if no data exists
        """
        stmt = select(func.max(HistoricalPrice.date)).where(
            HistoricalPrice.symbol == symbol.upper()
        )
        result = self._db.session.execute(stmt).scalar()
        if result is None:
            return None
        # Result may be a date or datetime depending on DB
        if isinstance(result, datetime):
            return result.date()
        return result

    def get_last_intraday_timestamp(self, symbol: str, interval: str) -> datetime | None:
        """Get the most recent timestamp for intraday data.

        Args:
            symbol: Stock symbol
            interval: Bar interval (e.g., "5min")

        Returns:
            Most recent timestamp or None if no data exists
        """
        try:
            Model = get_intraday_model(interval)
        except ValueError:
            return None

        stmt = select(func.max(Model.timestamp)).where(Model.symbol == symbol.upper())
        result = self._db.session.execute(stmt).scalar()
        return result
