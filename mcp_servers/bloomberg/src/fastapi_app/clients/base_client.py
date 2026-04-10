"""Base client interface for all data clients (OpenBB, FMP, Mock, Offline)."""

from datetime import datetime
from typing import Any, Protocol

import pandas as pd


class BloombergClient(Protocol):
    """Protocol defining the interface that all data clients must implement.

    This interface ensures consistent method signatures across:
    - OpenBBClient (yfinance-based)
    - FMPClient (Financial Modeling Prep API)
    - MockOpenBBClient (testing)
    - OfflineClient (local DuckDB)
    """

    async def fetch_quote(self, ticker: str) -> dict[str, Any]:
        """Fetch current quote data for a ticker.

        Args:
            ticker: Stock ticker symbol (e.g., "AAPL")

        Returns:
            Dictionary with quote data including:
            - last_price: Current/last price
            - bid: Bid price
            - ask: Ask price
            - open: Opening price
            - high: Day high
            - low: Day low
            - volume: Trading volume
        """
        ...

    async def fetch_historical_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = "1d",
        adjust_splits: bool = True,
        adjust_dividends: bool = True,
    ) -> pd.DataFrame:
        """Fetch historical OHLCV data.

        Args:
            symbol: Stock ticker symbol
            start_date: Start date for historical data
            end_date: End date for historical data
            interval: Data interval (1d, 1wk, 1mo, etc.)
            adjust_splits: Whether to adjust for stock splits
            adjust_dividends: Whether to adjust for dividends

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        ...

    async def fetch_intraday_bars(
        self, ticker: str, interval: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Fetch intraday bar data.

        Args:
            ticker: Stock ticker symbol
            interval: Bar interval (1m, 5m, 15m, 30m, 1h)
            start: Start datetime
            end: End datetime

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        ...

    async def fetch_intraday_ticks(
        self,
        ticker: str,
        event_types: list[str],
        start: datetime,
        end: datetime,
        include_condition_codes: bool = False,
        include_exchange_codes: bool = False,
        include_broker_codes: bool = False,
        include_spread_price: bool = False,
        include_yield: bool = False,
    ) -> dict[str, Any]:
        """Fetch intraday tick data.

        Args:
            ticker: Stock ticker symbol
            event_types: List of event types (TRADE, BID, ASK, etc.)
            start: Start datetime
            end: End datetime
            include_condition_codes: Include condition codes in response
            include_exchange_codes: Include exchange codes in response
            include_broker_codes: Include broker codes in response
            include_spread_price: Include spread price in response
            include_yield: Include yield in response

        Returns:
            Dictionary with 'eidData' and 'tickData' keys
        """
        ...
