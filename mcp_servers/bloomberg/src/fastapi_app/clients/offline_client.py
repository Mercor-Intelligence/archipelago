"""Offline Client - serves data from local DuckDB database.

This client wraps the DuckDBService and provides the same interface
as FMPClient/OfflineDataClient for seamless integration.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from db.service import DuckDBService

logger = logging.getLogger(__name__)


def has_duckdb_available(duckdb_path: str | Path) -> bool:
    """
    Check if DuckDB offline database is available.

    Args:
        duckdb_path: Path to DuckDB database file

    Returns:
        True if DuckDB file exists and has data
    """
    path = Path(duckdb_path)
    if not path.exists():
        return False

    # Check if file has reasonable size (at least 1KB = has some data)
    try:
        return path.stat().st_size > 1024
    except OSError:
        return False


class OfflineClient:
    """
    Client that serves data from local DuckDB database.

    Provides the same interface as FMPClient/OfflineDataClient:
    - fetch_quote
    - fetch_historical_data
    - fetch_intraday_bars
    - fetch_intraday_ticks
    - fetch_screener
    """

    def __init__(self, db_path: Path | str):
        """
        Initialize Offline client.

        Args:
            db_path: Path to DuckDB database file
        """
        self.db_path = Path(db_path)
        self._service = DuckDBService(db_path)

        if not self.db_path.exists():
            logger.warning(f"DuckDB path does not exist: {self.db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        self._service.close()

    async def fetch_quote(self, ticker: str) -> dict[str, Any]:
        """
        Fetch quote data from DuckDB.

        Args:
            ticker: Ticker symbol

        Returns:
            Dictionary with quote data (last_price, open, high, low, volume)
        """
        result = self._service.get_latest_price(ticker)
        if result is None:
            logger.warning(f"No data available for ticker: {ticker}")
            return {}

        return {
            "symbol": result.get("symbol"),
            "last_price": result.get("close") or result.get("adj_close"),
            "open": result.get("open"),
            "high": result.get("high"),
            "low": result.get("low"),
            "volume": result.get("volume"),
        }

    async def fetch_historical_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = "1d",
        adjust_splits: bool = True,
        adjust_dividends: bool = True,
        provider: str | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Fetch historical data from DuckDB.

        Args:
            symbol: Ticker symbol
            start_date: Start date for filtering
            end_date: End date for filtering
            interval: Data interval (ignored - DuckDB has daily data)
            adjust_splits: Ignored (data is pre-adjusted)
            adjust_dividends: Ignored (data is pre-adjusted)

        Returns:
            DataFrame with historical OHLCV data
        """
        df = self._service.get_historical(symbol, start_date, end_date)

        if df.empty:
            logger.warning(f"No historical data for {symbol}")
            return pd.DataFrame()

        logger.info(f"Loaded {len(df)} rows from DuckDB for {symbol}")
        return df

    async def fetch_intraday_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """
        Fetch intraday bar data from DuckDB.

        Args:
            ticker: Ticker symbol
            interval: Bar interval (e.g., "5m", "1m", "15m")
            start: Start datetime for filtering
            end: End datetime for filtering

        Returns:
            DataFrame with OHLCV bar data (date as index)
        """

        try:
            df = self._service.get_intraday_bars(ticker, interval, start, end)
        except ValueError as e:
            logger.warning(f"Intraday bars error for {ticker}: {e}")
            return pd.DataFrame()

        if df.empty:
            logger.warning(f"No intraday data for {ticker}")
            return pd.DataFrame()

        logger.info(f"Loaded {len(df)} intraday bars from DuckDB for {ticker}")
        return df

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
        Fetch intraday tick data from DuckDB.

        Uses 1-minute bars as tick data (same approach as FMPClient).

        Args:
            ticker: Ticker symbol
            event_types: Event types to filter (TRADE, BID, ASK)
            start: Start datetime
            end: End datetime
            Other args are ignored

        Returns:
            Dictionary with eidData and tickData
        """
        logger.info(f"Fetching tick data for {ticker} using 1-minute bars")

        try:
            df = self._service.get_intraday_bars(ticker, "1min", start, end)
        except ValueError:
            logger.warning(f"No 1-minute bar data for {ticker}")
            return {"ticker": ticker, "eidData": [], "tickData": [], "error": None}

        if df.empty:
            return {"ticker": ticker, "eidData": [], "tickData": [], "error": None}

        # Convert 1-minute bars to tick format (like FMPClient)
        tick_data = []

        def safe_float(val: Any) -> float:
            """Convert to float, handling NaN and None."""
            if val is None or pd.isna(val):
                return 0.0
            return float(val)

        def safe_int(val: Any) -> int:
            """Convert to int, handling NaN and None."""
            if val is None or pd.isna(val):
                return 0
            return int(val)

        for idx, row in df.iterrows():
            timestamp = pd.Timestamp(idx)  # type: ignore[arg-type]
            # Convert to UTC and format as Z-suffix ISO string
            timestamp_utc = (
                timestamp.tz_convert("UTC") if timestamp.tzinfo else timestamp.tz_localize("UTC")
            )
            time_str = timestamp_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

            if "TRADE" in event_types or "ALL" in event_types:
                tick_data.append(
                    {
                        "time": time_str,
                        "type": "TRADE",
                        "value": safe_float(row.get("close")),
                        "size": safe_int(row.get("volume")),
                        "conditionCodes": None,
                        "exchangeCode": None,
                        "brokerCode": None,
                        "spreadPrice": None,
                        "yield": None,
                    }
                )

            if "BID" in event_types:
                tick_data.append(
                    {
                        "time": time_str,
                        "type": "BID",
                        "value": safe_float(row.get("low")),
                        "size": 0,
                        "conditionCodes": None,
                        "exchangeCode": None,
                        "brokerCode": None,
                        "spreadPrice": None,
                        "yield": None,
                    }
                )

            if "ASK" in event_types:
                tick_data.append(
                    {
                        "time": time_str,
                        "type": "ASK",
                        "value": safe_float(row.get("high")),
                        "size": 0,
                        "conditionCodes": None,
                        "exchangeCode": None,
                        "brokerCode": None,
                        "spreadPrice": None,
                        "yield": None,
                    }
                )

        logger.info(f"Generated {len(tick_data)} ticks for {ticker}")
        return {"ticker": ticker, "eidData": [], "tickData": tick_data, "error": None}

    async def fetch_screener(
        self,
        sector: str | None = None,
        market_cap_min: float | None = None,
        market_cap_max: float | None = None,
        **kwargs,
    ) -> list[dict]:
        """
        Fetch screener data from DuckDB company_profiles table.

        Args:
            sector: Sector filter (e.g., "Technology")
            market_cap_min: Minimum market cap in millions (e.g., 1000 = $1B)
            market_cap_max: Maximum market cap in millions

        Returns:
            List of company profiles matching criteria
        """
        from db.models.company_profile import CompanyProfile

        try:
            # Convert market cap from millions to actual value
            mc_min = int(market_cap_min * 1_000_000) if market_cap_min is not None else None
            mc_max = int(market_cap_max * 1_000_000) if market_cap_max is not None else None

            profiles = CompanyProfile.screen(
                self._service.session,
                sector=sector,
                market_cap_min=mc_min,
                market_cap_max=mc_max,
                is_actively_trading=True,
            )

            if profiles:
                results = [p.to_dict() for p in profiles]
                logger.info(f"Screener returned {len(results)} profiles from DuckDB")
                return results

            # No profiles found - fall through to fallback
            logger.info("No profiles in database, falling back to symbol list")

        except Exception as e:
            logger.warning(f"Error querying profiles, falling back to symbols: {e}")

        # Fallback to basic symbol list if profiles not seeded
        symbols = self._service.get_symbols()
        return [
            {
                "symbol": symbol,
                "companyName": symbol,
                "sector": "Unknown",
                "industry": "Unknown",
                "exchange": "Unknown",
                "exchangeShortName": "",
                "marketCap": None,
                "price": None,
                "beta": None,
                "volume": None,
                "lastAnnualDividend": None,
                "country": "",
                "isEtf": False,
                "isActivelyTrading": True,
            }
            for symbol in symbols
        ]

    def is_available(self, symbol: str) -> bool:
        """Check if data is available for a symbol."""
        return self._service.is_available(symbol)

    def list_available_symbols(self) -> list[str]:
        """Return list of available symbols."""
        return self._service.get_symbols()
