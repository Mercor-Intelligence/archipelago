import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd


class MockOpenBBClient:
    """
    Mock OpenBB client for runtime use with USE_MOCK=true.

    Provides deterministic mock data for both quote and intraday bar requests
    without making actual network calls to data providers.

    Implements the BloombergClient interface for testing.
    """

    # Base prices for common tickers (same as test factory for consistency)
    TICKER_BASE_PRICES = {
        "AAPL": 262.82,
        "IBM": 185.42,
        "MSFT": 420.55,
        "GOOGL": 2850.50,
        "AMZN": 3485.20,
        "TSLA": 245.67,
        "META": 512.34,
        "NVDA": 875.45,
    }

    def __init__(self):
        self._mock_data: dict[str, pd.DataFrame] = {}
        self._mock_tick_data: dict[str, list[dict[str, Any]]] = {}

    def set_mock_data(self, ticker: str, data: pd.DataFrame):
        """
        Set custom mock data for a given ticker.

        Args:
            ticker (str): Stock ticker symbol (e.g., "AAPL").
            data (pd.DataFrame): DataFrame with columns like Open, High, Low, Close, Volume.
        """
        self._mock_data[ticker.upper()] = data

    async def fetch_intraday_bars(
        self,
        ticker: str,
        interval: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        # Simulate async delay
        await asyncio.sleep(0.05)
        ticker = ticker.upper()

        # If mock data exists, return it
        if ticker in self._mock_data:
            return self._mock_data[ticker]

        # Otherwise, generate synthetic data
        if not start:
            start = datetime.now(UTC) - timedelta(hours=5)
        if not end:
            end = datetime.now(UTC)

        timestamps = pd.date_range(start=start, end=end, freq=interval)

        data = {
            "Open": [100 + i * 0.5 for i in range(len(timestamps))],
            "High": [100 + i * 0.6 for i in range(len(timestamps))],
            "Low": [100 + i * 0.4 for i in range(len(timestamps))],
            "Close": [100 + i * 0.55 for i in range(len(timestamps))],
            "Volume": [1000 + i * 10 for i in range(len(timestamps))],
        }

        df = pd.DataFrame(data, index=timestamps)
        df.index.name = "Datetime"
        return df

    async def fetch_quote(self, ticker: str) -> dict[str, Any]:
        """
        Fetch mock quote data for a ticker.

        Args:
            ticker: Stock ticker symbol (e.g., "AAPL")

        Returns:
            Dictionary with quote data (last_price, bid, ask, volume, etc.)

        Example:
            >>> client = MockOpenBBClient()
            >>> quote = await client.fetch_quote("AAPL")
            >>> quote["last_price"]
            262.82
        """
        # Simulate async delay
        await asyncio.sleep(0.05)

        ticker_upper = ticker.upper()

        # Use deterministic seed based on ticker for reproducibility
        seed = abs(hash(ticker_upper)) % 100000
        rng = random.Random(seed)

        # Get base price for ticker
        base_price = self.TICKER_BASE_PRICES.get(ticker_upper, 150.0)

        # Generate realistic spread (10 basis points default)
        spread_bps = 10.0
        half_spread = (base_price * spread_bps / 10000.0) / 2.0

        quote_data = {
            "last_price": base_price,
            "bid": round(base_price - half_spread, 2),
            "ask": round(base_price + half_spread, 2),
            "bid_size": rng.randint(1, 10),
            "ask_size": rng.randint(1, 10),
            "open": round(base_price - rng.uniform(0.5, 2.0), 2),
            "high": round(base_price + rng.uniform(1.0, 3.0), 2),
            "low": round(base_price - rng.uniform(2.0, 5.0), 2),
            "volume": rng.randint(1000000, 50000000),
        }

        return quote_data

    async def fetch_historical_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = "1d",
        adjust_splits: bool = True,
        adjust_dividends: bool = True,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data for a ticker (mock implementation).

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL")
            start_date: Start date for historical data
            end_date: End date for historical data
            interval: Data interval (1d, 1wk, 1mo, etc.)
            adjust_splits: Whether to adjust for stock splits
            adjust_dividends: Whether to adjust for dividends

        Returns:
            DataFrame with historical OHLCV data
        """
        # Simulate async delay
        await asyncio.sleep(0.05)

        symbol = symbol.upper()

        # If mock data exists, return it
        if symbol in self._mock_data:
            return self._mock_data[symbol]

        # Otherwise, generate synthetic data
        timestamps = pd.date_range(start=start_date, end=end_date, freq=interval)

        close_prices = [100 + i * 0.55 for i in range(len(timestamps))]

        data = {
            "Open": [100 + i * 0.5 for i in range(len(timestamps))],
            "High": [100 + i * 0.6 for i in range(len(timestamps))],
            "Low": [100 + i * 0.4 for i in range(len(timestamps))],
            "Close": close_prices,
            "adjclose": close_prices,  # Adjusted close (same as close for mock data)
            "Last_Price": close_prices,  # For historical data, last_price = close price
            "Volume": [1000 + i * 10 for i in range(len(timestamps))],
        }

        df = pd.DataFrame(data, index=timestamps)
        df.index.name = "Date"
        return df

    def set_mock_tick_data(self, ticker: str, tick_data: list[dict[str, Any]]):
        """
        Set custom mock tick data for a given ticker.

        Args:
            ticker (str): Stock ticker symbol (e.g., "AAPL").
            tick_data (list[dict]): List of tick dictionaries with time, type, value, size, etc.
        """
        self._mock_tick_data[ticker.upper()] = tick_data

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
        Generate mock intraday tick data.

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
        """
        # Simulate async delay
        await asyncio.sleep(0.05)
        ticker = ticker.upper()

        # If mock tick data exists, return it (assuming it's in the full format)
        if ticker in self._mock_tick_data:
            # If custom data is just a list, wrap it in the proper format
            custom_data = self._mock_tick_data[ticker]
            if isinstance(custom_data, list):
                return {
                    "eidData": [
                        {
                            "EID": f"MOCK_{ticker}_EID",
                            "description": f"Mock Exchange ID for {ticker}",
                        }
                    ],
                    "tickData": custom_data,
                }
            return custom_data

        # Otherwise, generate synthetic tick data
        ticks = []
        current_time = start
        base_price = 182.00

        # Generate 20-50 ticks
        num_ticks = random.randint(20, 50)
        time_delta = (end - start) / num_ticks

        for i in range(num_ticks):
            # Cycle through requested event types
            event_type = event_types[i % len(event_types)]

            # Generate realistic tick data
            price_variation = random.uniform(-2.0, 2.0)
            current_price = base_price + price_variation
            size = random.randint(50, 500) * 100

            tick = {
                "time": current_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "type": event_type,
                "value": round(current_price, 2),
                "size": size,
            }

            # Add optional fields based on parameters
            if include_condition_codes and event_type == "TRADE":
                tick["conditionCodes"] = "XT"
            if include_exchange_codes:
                tick["exchangeCode"] = "NSQ"
            if include_broker_codes:
                tick["brokerCode"] = random.choice(["GSCO", "MSCO", "JPMQ"])
            if include_spread_price:
                tick["spreadPrice"] = round(random.uniform(0.01, 0.05), 3)
            if include_yield:
                tick["yield"] = round(random.uniform(1.5, 3.5), 2)

            ticks.append(tick)
            current_time += time_delta

            # Vary base price slightly for next tick
            base_price += random.uniform(-0.1, 0.1)

        # Generate mock eidData (this would come from Bloomberg in real implementation)
        eid_data = [
            {
                "EID": f"{random.randint(100000, 999999)}",
                "description": f"Bloomberg event ID for {ticker}",
            }
        ]

        return {"eidData": eid_data, "tickData": ticks}


# Singleton instance
mock_openbb_client = MockOpenBBClient()
