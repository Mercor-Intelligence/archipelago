"""FMP (Financial Modeling Prep) API fetcher."""

import os
import time
from datetime import date, datetime, timedelta

import httpx

from .base import BaseFetcher

# Default: 5 requests per second (300 per minute)
DEFAULT_REQUESTS_PER_SECOND = 25.0


class FMPFetcher(BaseFetcher):
    """Fetcher for Financial Modeling Prep API (uses stable API)."""

    BASE_URL = "https://financialmodelingprep.com/stable"

    def __init__(
        self,
        api_key: str | None = None,
        requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    ):
        self.api_key = api_key or self._get_api_key()
        self._client = httpx.Client(timeout=30.0)
        self._min_interval = 1.0 / requests_per_second
        self._last_request_time: float = 0.0

    @staticmethod
    def _get_api_key() -> str:
        """Get API key from environment variables."""
        api_key = os.environ.get("FMP_API_KEY")
        if not api_key:
            raise ValueError("FMP API key not found. Set FMP_API_KEY environment variable.")
        return api_key

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def _throttle(self) -> None:
        """Enforce rate limiting between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()

    def _get(self, url: str, params: dict) -> httpx.Response:
        """Make a throttled GET request."""
        self._throttle()
        response = self._client.get(url, params=params)
        response.raise_for_status()
        return response

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
            Raw API response wrapped in dict with metadata
        """
        # Determine date range
        if to_date is None:
            to_date = date.today()
        elif isinstance(to_date, datetime):
            to_date = to_date.date()

        if from_date is not None:
            if isinstance(from_date, datetime):
                from_date = from_date.date()
            start_date = from_date
        elif days is not None:
            start_date = to_date - timedelta(days=days)
        else:
            # Default to 90 days if nothing specified
            start_date = to_date - timedelta(days=90)

        # Stable API uses query parameter for symbol
        url = f"{self.BASE_URL}/historical-price-eod/dividend-adjusted"
        params = {
            "symbol": symbol,
            "apikey": self.api_key,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": to_date.strftime("%Y-%m-%d"),
        }

        response = self._get(url, params)
        raw_data = response.json()

        # Stable API returns flat array with adjusted field names
        # Map field names for compatibility with existing code
        historical = []
        for row in raw_data:
            historical.append(
                {
                    "date": row.get("date"),
                    "open": row.get("adjOpen"),
                    "high": row.get("adjHigh"),
                    "low": row.get("adjLow"),
                    "close": row.get("adjClose"),
                    "volume": row.get("volume"),
                    "symbol": row.get("symbol"),
                }
            )

        # Return in wrapped format for compatibility
        data = {"symbol": symbol, "historical": historical}

        return {
            "source": "fmp",
            "endpoint": "/historical-price-eod/dividend-adjusted",
            "symbol": symbol,
            "params": {
                "from_date": start_date.isoformat(),
                "to_date": to_date.isoformat(),
            },
            "fetched_at": datetime.now().isoformat(),
            "data": data,
        }

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
            Raw API response wrapped in dict with metadata

        Note:
            FMP doesn't support date filtering for intraday, so we fetch all
            available data and filter client-side if from_timestamp is provided.
        """
        # Stable API uses query parameter for symbol
        url = f"{self.BASE_URL}/historical-chart/{interval}"
        params = {"symbol": symbol, "apikey": self.api_key}

        response = self._get(url, params)
        data = response.json()

        # FMP returns a list directly for intraday, normalize to dict
        bars = data if isinstance(data, list) else []

        # Filter bars if from_timestamp provided (client-side filtering)
        if from_timestamp is not None and bars:
            filtered_bars = []
            for bar in bars:
                bar_ts = datetime.fromisoformat(bar["date"])
                if bar_ts > from_timestamp:
                    filtered_bars.append(bar)
            bars = filtered_bars

        return {
            "source": "fmp",
            "endpoint": f"/historical-chart/{interval}",
            "symbol": symbol,
            "params": {
                "interval": interval,
                "from_timestamp": from_timestamp.isoformat() if from_timestamp else None,
            },
            "fetched_at": datetime.now().isoformat(),
            "data": {"bars": bars},
        }

    def fetch_profile(self, symbol: str) -> dict:
        """Fetch company profile for a single symbol.

        Args:
            symbol: Stock symbol (e.g., "AAPL")

        Returns:
            Raw API response wrapped in dict with metadata
        """
        # Stable API uses query parameter for symbol
        url = f"{self.BASE_URL}/profile"
        params = {"symbol": symbol, "apikey": self.api_key}

        response = self._get(url, params)
        data = response.json()

        profile = data[0] if data and len(data) > 0 else None

        return {
            "source": "fmp",
            "endpoint": "/profile",
            "symbol": symbol,
            "fetched_at": datetime.now().isoformat(),
            "data": {"profile": profile},
        }

    def fetch_profiles_batch(self, symbols: list[str], batch_size: int = 50) -> dict:
        """Fetch profiles for multiple symbols in batches.

        Args:
            symbols: List of stock symbols
            batch_size: Number of symbols per request (max ~50 for URL length)

        Returns:
            Raw API response wrapped in dict with metadata
        """
        all_profiles = []

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            symbols_str = ",".join(batch)

            # Stable API uses query parameter for symbol (supports comma-separated)
            url = f"{self.BASE_URL}/profile"
            params = {"symbol": symbols_str, "apikey": self.api_key}

            response = self._get(url, params)
            data = response.json()

            if data:
                all_profiles.extend(data)

        return {
            "source": "fmp",
            "endpoint": "/profile",
            "symbols": symbols,
            "fetched_at": datetime.now().isoformat(),
            "data": {"profiles": all_profiles},
        }

    def fetch_sp500_constituents(self) -> list[str]:
        """Fetch list of S&P 500 constituent symbols.

        Returns:
            List of stock symbols
        """
        # Stable API uses hyphen instead of underscore
        url = f"{self.BASE_URL}/sp500-constituent"
        params = {"apikey": self.api_key}

        response = self._get(url, params)
        data = response.json()

        return [item["symbol"] for item in data]
