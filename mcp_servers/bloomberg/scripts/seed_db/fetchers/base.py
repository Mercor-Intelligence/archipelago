"""Base fetcher interface for data retrieval."""

from abc import ABC, abstractmethod
from datetime import date, datetime


class BaseFetcher(ABC):
    """Abstract base class for data fetchers."""

    @abstractmethod
    def fetch_historical(
        self,
        symbol: str,
        days: int | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> dict:
        """Fetch historical daily OHLCV data.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            days: Number of days of history to fetch (used if from_date not provided)
            from_date: Start date for fetching (inclusive)
            to_date: End date for fetching (inclusive), defaults to today

        Returns:
            Raw API response as dict with 'historical' key containing list of OHLCV data
        """
        pass

    @abstractmethod
    def fetch_intraday(
        self,
        symbol: str,
        interval: str,
        from_timestamp: datetime | None = None,
    ) -> dict:
        """Fetch intraday bar data.

        Args:
            symbol: Stock symbol (e.g., "AAPL")
            interval: Bar interval (e.g., "5min", "15min", "1hour")
            from_timestamp: Only return bars after this timestamp (for incremental)

        Returns:
            Raw API response as dict with 'bars' key containing list of OHLCV data
        """
        pass

    @abstractmethod
    def fetch_profile(self, symbol: str) -> dict:
        """Fetch company profile data.

        Args:
            symbol: Stock symbol (e.g., "AAPL")

        Returns:
            Raw API response as dict with profile data
        """
        pass

    @abstractmethod
    def fetch_profiles_batch(self, symbols: list[str]) -> dict:
        """Fetch company profiles for multiple symbols.

        Args:
            symbols: List of stock symbols

        Returns:
            Raw API response as dict with 'profiles' key containing list of profiles
        """
        pass

    def close(self) -> None:  # noqa: B027
        """Close any resources. Override if needed."""
        pass

    def __enter__(self) -> "BaseFetcher":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
