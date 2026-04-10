"""OpenBB Client for fetching data from providers."""

import asyncio
import logging
from datetime import datetime
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class OpenBBClient:
    """
    Client for fetching market data from providers (yfinance).
    Handles network calls, retries, and error handling.

    Implements the BloombergClient interface for data fetching.
    """

    def __init__(self):
        """
        Initialize OpenBB client
        """

    def _fetch_quote_sync(self, ticker: str) -> dict[str, Any]:
        """Synchronous helper to fetch quote data using yfinance.

        This is a blocking call that should be run in a thread pool.

        Args:
            ticker: Stock ticker symbol

        Returns:
            Dictionary with raw quote data
        """
        stock = yf.Ticker(ticker)
        info = stock.info

        # Try to get current price from fast_info first, fall back to info
        try:
            fast_info = stock.fast_info
            current_price = fast_info.get("last_price")
            if current_price is None:
                current_price = info.get("currentPrice")
        except Exception:
            current_price = info.get("currentPrice")

        quote_data = {
            "last_price": current_price,
            "bid": info.get("bid"),
            "ask": info.get("ask"),
            "bid_size": info.get("bidSize"),
            "ask_size": info.get("askSize"),
            "open": info.get("open"),
            "high": info.get("dayHigh"),
            "low": info.get("dayLow"),
            "volume": info.get("volume"),
        }

        return quote_data

    async def fetch_quote(self, ticker: str) -> dict[str, Any]:
        """Fetch raw quote data for a ticker using yfinance.

        Runs the blocking yfinance call in a thread pool to keep the event loop responsive.

        Args:
            ticker: Stock ticker symbol (e.g., "AAPL")

        Returns:
            Dictionary with raw quote data from provider

        Raises:
            Exception: If data fetching fails
        """
        try:
            # Run the blocking yfinance call in a thread pool
            quote_data = await asyncio.to_thread(self._fetch_quote_sync, ticker)
            logger.info(f"Fetched quote for {ticker}: {quote_data}")
            return quote_data

        except Exception as e:
            logger.error(f"Error fetching quote for {ticker}: {e}")
            raise

    def _fetch_historical_data_sync(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = "1d",
        adjust_splits: bool = True,
        adjust_dividends: bool = True,
    ) -> pd.DataFrame:
        """Synchronous helper to fetch historical data using yfinance.

        This is a blocking call that should be run in a thread pool.

        Args:
            symbol: Stock ticker symbol
            start_date: Start date for historical data
            end_date: End date for historical data
            interval: Data interval (1d, 1wk, 1mo, etc.)
            adjust_splits: Whether to adjust for stock splits
            adjust_dividends: Whether to adjust for dividends

        Returns:
            DataFrame with historical OHLCV data

        Raises:
            ValueError: If no data is available for the given parameters
        """
        ticker = yf.Ticker(symbol)

        # Fetch historical data
        df = ticker.history(
            start=start_date,
            end=end_date,
            interval=interval,
            auto_adjust=False,  # We'll handle adjustments manually
        )

        if df.empty:
            raise ValueError(f"No data available for {symbol} between {start_date} and {end_date}")

        # Apply adjustments if requested
        if adjust_splits or adjust_dividends:
            # Get adjustment factors
            if adjust_splits and "Stock Splits" in df.columns:
                split_factor = (1 / df["Stock Splits"].replace(0, 1)).cumprod()
                for col in ["Open", "High", "Low", "Close"]:
                    if col in df.columns:
                        df[col] = df[col] / split_factor
                if "Volume" in df.columns:
                    df["Volume"] = df["Volume"] * split_factor

            if adjust_dividends and "Dividends" in df.columns:
                # Adjust for dividends
                # For first row, use current close (no previous close exists)
                # This prevents NaN propagation from shift(1)
                prev_close = df["Close"].shift(1).fillna(df["Close"])
                # type: ignore[attr-defined] - pandas Series has cumprod method
                dividend_adjustment = (1 - df["Dividends"] / prev_close).cumprod()  # type: ignore[attr-defined]
                # Divide by adjustment factor to back-adjust historical prices
                for col in ["Open", "High", "Low", "Close"]:
                    if col in df.columns:
                        df[col] = df[col] / dividend_adjustment

        return df

    async def fetch_historical_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = "1d",
        adjust_splits: bool = True,
        adjust_dividends: bool = True,
    ) -> pd.DataFrame:
        """Fetch historical OHLCV data for a ticker using yfinance.

        Runs the blocking yfinance call in a thread pool to keep the event loop responsive.

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL")
            start_date: Start date for historical data
            end_date: End date for historical data
            interval: Data interval (1d, 1wk, 1mo, etc.)
            adjust_splits: Whether to adjust for stock splits
            adjust_dividends: Whether to adjust for dividends

        Returns:
            DataFrame with historical OHLCV data

        Raises:
            ValueError: If no data is available for the given parameters
            Exception: If data fetching fails
        """
        try:
            # Run the blocking yfinance call in a thread pool
            df = await asyncio.to_thread(
                self._fetch_historical_data_sync,
                symbol,
                start_date,
                end_date,
                interval,
                adjust_splits,
                adjust_dividends,
            )
            logger.info(f"Fetched {len(df)} rows of historical data for {symbol}")
            return df

        except Exception as e:
            logger.error(f"Error fetching historical data for {symbol}: {e}")
            raise

    async def fetch_intraday_bars(
        self: "OpenBBClient",
        ticker: str,
        interval: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Fetch intraday bar data for a ticker using yfinance.

        Args:
            ticker: Stock ticker symbol (e.g., "AAPL")
            interval: Data interval (e.g., "1m", "5m", "1h")
            start: Start datetime for intraday data
            end: End datetime for intraday data

        Returns:
            DataFrame with intraday OHLCV data
        """

        def get_data():
            stock = yf.Ticker(ticker)
            df = stock.history(interval=interval, start=start, end=end)
            df.index = pd.to_datetime(df.index)
            return df

        return await asyncio.to_thread(get_data)

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
        """
        Fetch intraday tick data.

        Note: This is a placeholder for real Bloomberg API integration.
        For now, it raises NotImplementedError.

        Args:
            ticker: Stock ticker symbol
            event_types: List of event types (TRADE, BID, ASK, etc.)
            start: Start datetime
            end: End datetime
            include_condition_codes: Include condition codes
            include_exchange_codes: Include exchange codes
            include_broker_codes: Include broker codes
            include_spread_price: Include spread price
            include_yield: Include yield

        Returns:
            Dictionary with 'eidData' and 'tickData' keys

        Raises:
            NotImplementedError: Real Bloomberg tick data not yet implemented
        """
        raise NotImplementedError(
            "Real Bloomberg tick data integration not yet implemented. "
            "Use MockOpenBBClient for testing."
        )
