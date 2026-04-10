"""Base loader interface for data insertion."""

from abc import ABC, abstractmethod
from datetime import date, datetime


class BaseLoader(ABC):
    """Abstract base class for data loaders."""

    @abstractmethod
    def load_historical(self, symbol: str, raw_data: dict) -> int:
        """Load historical data from raw format into storage.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            raw_data: Raw data dict from fetcher/storage with 'data' key

        Returns:
            Number of rows inserted
        """
        pass

    @abstractmethod
    def load_intraday(self, symbol: str, interval: str, raw_data: dict) -> int:
        """Load intraday data from raw format into storage.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            interval: Bar interval (e.g., "5min", "15min", "1hour")
            raw_data: Raw data dict from fetcher/storage with 'data' key

        Returns:
            Number of rows inserted
        """
        pass

    @abstractmethod
    def load_profile(self, symbol: str, raw_data: dict) -> int:
        """Load company profile from raw format into storage.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            raw_data: Raw data dict from fetcher/storage with 'data' key

        Returns:
            Number of rows inserted (0 or 1)
        """
        pass

    @abstractmethod
    def load_profiles_batch(self, raw_data: dict) -> int:
        """Load multiple company profiles from raw format into storage.

        Args:
            raw_data: Raw data dict from fetcher/storage with 'data.profiles' list

        Returns:
            Number of profiles inserted
        """
        pass

    @abstractmethod
    def needs_loading(self, symbol: str, data_type: str) -> tuple[bool, str]:
        """Check if data needs to be loaded for a symbol.

        Args:
            symbol: Stock symbol
            data_type: Type of data ('historical', 'intraday_5min', etc.)

        Returns:
            Tuple of (needs_loading, reason_string)
        """
        pass

    @abstractmethod
    def get_profile_symbols(self) -> set[str]:
        """Get set of symbols that have profiles loaded.

        Returns:
            Set of symbol strings
        """
        pass

    @abstractmethod
    def get_last_historical_date(self, symbol: str) -> date | None:
        """Get the most recent date for historical data.

        Args:
            symbol: Stock symbol

        Returns:
            Most recent date or None if no data exists
        """
        pass

    @abstractmethod
    def get_last_intraday_timestamp(self, symbol: str, interval: str) -> datetime | None:
        """Get the most recent timestamp for intraday data.

        Args:
            symbol: Stock symbol
            interval: Bar interval (e.g., "5min")

        Returns:
            Most recent timestamp or None if no data exists
        """
        pass

    def close(self) -> None:  # noqa: B027
        """Close any resources. Override if needed."""
        pass

    def __enter__(self) -> "BaseLoader":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
