"""Direct FMP API client - bypasses OpenBB Platform entirely.

This client mirrors the v1-gui FMP adapter but in Python.
Calls FMP REST API directly without OpenBB wrapper.
"""

import logging
from datetime import datetime
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)


class FMPClient:
    """Direct FMP API client for financial data.

    Implements the BloombergClient interface for data fetching.
    Uses FMP stable API endpoints.
    """

    def __init__(self, api_key: str | None = None):
        """Initialize FMP client with API key."""
        self.api_key = api_key
        self.base_url = "https://financialmodelingprep.com/stable"

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        pass

    async def close(self):
        """Close the HTTP client (no-op — clients are per-request now)."""
        pass

    def _new_client(self) -> httpx.AsyncClient:
        """Create a fresh HTTP client for each request.

        Avoids stale TCP DNS connections when resolv.conf uses
        'options use-vc' (DNS over TCP).
        """
        return httpx.AsyncClient(timeout=30.0)

    async def _get(self, url: str, params: dict) -> httpx.Response:
        """Make a GET request using a fresh HTTP client."""
        async with self._new_client() as client:
            return await client.get(url, params=params)

    async def fetch_quote(self, ticker: str) -> dict[str, Any]:
        """Fetch real-time quote data.

        Endpoint: GET /quote?symbol={ticker} (stable API)
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/quote"
        params = {"symbol": ticker, "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()

            if not data or len(data) == 0:
                logger.warning(f"No quote data found for {ticker}")
                return {}

            quote = data[0]

            # Map FMP fields to OpenBB-like format
            return {
                "symbol": quote.get("symbol"),
                "last_price": quote.get("price"),
                "open": quote.get("open"),
                "high": quote.get("dayHigh"),
                "low": quote.get("dayLow"),
                "close": quote.get("previousClose"),
                "volume": quote.get("volume"),
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
            }

        except Exception as e:
            logger.error(f"Error fetching quote for {ticker}: {e}")
            raise

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

        Endpoint: GET /historical-price-eod/dividend-adjusted?symbol={symbol} (stable API)
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/historical-price-eod/dividend-adjusted"
        params = {
            "symbol": symbol,
            "apikey": self.api_key,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d"),
        }

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()

            if not data or len(data) == 0:
                logger.warning(f"No historical data found for {symbol}")
                return pd.DataFrame()

            # Stable API returns flat array with adjusted field names
            historical = data

            # Convert to DataFrame and map field names for compatibility
            df = pd.DataFrame(historical)
            # Map stable API field names to standard names
            field_mapping = {
                "adjOpen": "open",
                "adjHigh": "high",
                "adjLow": "low",
                "adjClose": "close",
            }
            df = df.rename(columns=field_mapping)

            # Reverse (FMP returns newest first)
            df = df.iloc[::-1].reset_index(drop=True)

            # Ensure date column is datetime
            df["date"] = pd.to_datetime(df["date"])

            # Filter to requested date range (defensive filtering)
            # Convert start_date and end_date to pandas Timestamp
            start_ts = pd.Timestamp(start_date)
            end_ts = pd.Timestamp(end_date)

            # Handle timezone-aware/naive comparison
            if df["date"].dt.tz is not None:
                # If column is timezone-aware, make timestamps timezone-aware
                if start_ts.tz is None:
                    start_ts = start_ts.tz_localize("UTC")
                if end_ts.tz is None:
                    end_ts = end_ts.tz_localize("UTC")
            else:
                # If column is timezone-naive, make timestamps timezone-naive
                if start_ts.tz is not None:
                    start_ts = start_ts.replace(tzinfo=None)
                if end_ts.tz is not None:
                    end_ts = end_ts.replace(tzinfo=None)

            df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)]

            logger.info(f"Filtered to {len(df)} days within requested range")
            return df

        except Exception as e:
            logger.error(f"Error fetching historical data for {symbol}: {e}")
            raise

    async def fetch_intraday_bars(
        self, ticker: str, interval: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Fetch intraday bar data.

        Endpoint: GET /historical-chart/{interval}/{symbol}

        FMP supports: 1min, 5min, 15min, 30min, 1hour, 4hour
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        # Map Bloomberg intervals to FMP intervals
        interval_map = {
            "1": "1min",
            "5": "5min",
            "15": "15min",
            "30": "30min",
            "60": "1hour",
            "1m": "1min",
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "1h": "1hour",
            "4h": "4hour",
        }

        fmp_interval = interval_map.get(str(interval), "1min")

        # Stable API uses query parameter for symbol instead of path parameter
        url = f"{self.base_url}/historical-chart/{fmp_interval}"
        # FMP only accepts date-only format for from/to params (time components cause issues)
        # We'll fetch the full day(s) and filter by time client-side
        params = {
            "symbol": ticker,
            "apikey": self.api_key,
            "from": start.strftime("%Y-%m-%d"),
            "to": end.strftime("%Y-%m-%d"),
        }

        logger.info(
            f"FMP intraday request for {ticker}: {url} with params from={params['from']} to={params['to']} interval={fmp_interval} (will filter to {start} - {end} client-side)"
        )

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()

            if not data or len(data) == 0:
                logger.warning(f"No intraday data found for {ticker}")
                return pd.DataFrame()

            # Convert to DataFrame
            df = pd.DataFrame(data)
            logger.info(f"FMP returned {len(df)} raw bars")

            # Ensure date column is datetime and set as index
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

            # Filter to requested date range (FMP sometimes returns data outside range)
            # Convert start/end to pandas Timestamp for proper comparison
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)

            logger.info(
                f"Filtering: df.index.tz={getattr(df.index, 'tz', None)}, "
                f"start_ts={start_ts} (tz={start_ts.tz}), end_ts={end_ts} (tz={end_ts.tz})"
            )
            if len(df) > 0:
                logger.info(f"DataFrame index range: {df.index.min()} to {df.index.max()}")

            # If index is timezone-aware, make timestamps timezone-aware too
            if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
                if start_ts.tz is None:
                    start_ts = start_ts.tz_localize("UTC")
                if end_ts.tz is None:
                    end_ts = end_ts.tz_localize("UTC")
            else:
                # If index is timezone-naive, make timestamps timezone-naive
                if start_ts.tz is not None:
                    start_ts = start_ts.replace(tzinfo=None)
                if end_ts.tz is not None:
                    end_ts = end_ts.replace(tzinfo=None)

            logger.info(f"After tz adjustment: start_ts={start_ts}, end_ts={end_ts}")

            df = df[(df.index >= start_ts) & (df.index <= end_ts)]

            # Reverse (FMP returns newest first)
            df = df.iloc[::-1]

            logger.info(f"Filtered to {len(df)} bars within requested range")
            return df

        except Exception as e:
            logger.error(f"Error fetching intraday bars for {ticker}: {e}")
            raise

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

        Uses 1-minute bars as tick data (same as v1-gui).
        Note: Bloomberg-specific fields (condition_codes, etc.) are accepted but not used,
        as FMP doesn't provide this level of detail.
        """
        logger.info(f"Fetching tick data for {ticker} using 1-minute bars")

        try:
            # Fetch 1-minute bars
            df = await self.fetch_intraday_bars(ticker, "1", start, end)

            if df.empty:
                logger.warning(f"No tick data found for {ticker}")
                return {"ticker": ticker, "eidData": [], "tickData": [], "error": None}

            # Convert 1-minute bars to tick format (like v1-gui)
            tick_data = []
            for timestamp, row in df.iterrows():
                # Convert timestamp to pd.Timestamp to ensure isoformat is available
                ts = pd.Timestamp(timestamp)  # type: ignore[arg-type]
                time_str = ts.isoformat()

                # Create TRADE ticks from close price
                if "TRADE" in event_types or "ALL" in event_types:
                    close_val = row.get("close")
                    volume_val = row.get("volume", 0)

                    # Skip tick if critical price data is missing
                    if close_val is not None and not pd.isna(close_val):  # type: ignore[arg-type]
                        # Ensure volume is valid (0 is acceptable for volume)
                        # Check for NaN using pd.isna on scalar value
                        if volume_val is None or pd.isna(volume_val):  # type: ignore[arg-type]
                            volume_val = 0

                        tick_data.append(
                            {
                                "time": time_str,
                                "type": "TRADE",
                                "value": float(close_val),  # type: ignore[arg-type]
                                "size": int(volume_val),  # type: ignore[arg-type]
                                "conditionCodes": None,
                                "exchangeCode": None,
                                "brokerCode": None,
                                "spreadPrice": None,
                                "yield": None,
                            }
                        )

                # Add BID ticks (approximate as low price)
                if "BID" in event_types:
                    low_val = row.get("low")

                    # Skip tick if critical price data is missing
                    if low_val is not None and not pd.isna(low_val):  # type: ignore[arg-type]
                        tick_data.append(
                            {
                                "time": time_str,
                                "type": "BID",
                                "value": float(low_val),  # type: ignore[arg-type]
                                "size": 0,
                                "conditionCodes": None,
                                "exchangeCode": None,
                                "brokerCode": None,
                                "spreadPrice": None,
                                "yield": None,
                            }
                        )

                # Add ASK ticks (approximate as high price)
                if "ASK" in event_types:
                    high_val = row.get("high")

                    # Skip tick if critical price data is missing
                    if high_val is not None and not pd.isna(high_val):  # type: ignore[arg-type]
                        tick_data.append(
                            {
                                "time": time_str,
                                "type": "ASK",
                                "value": float(high_val),  # type: ignore[arg-type]
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

        except Exception as e:
            logger.error(f"Error fetching intraday ticks for {ticker}: {e}")
            return {"ticker": ticker, "eidData": [], "tickData": [], "error": {"message": str(e)}}

    async def _fetch_statement(
        self,
        endpoint: str,
        ticker: str,
        period: str = "annual",
        offset: int = 0,
    ) -> dict[str, Any]:
        """Generic method to fetch financial statement data from FMP.

        Args:
            offset: 0 = most recent period, 1 = one period back, etc.

        Returns the selected period's data as a flat dict, or {} if unavailable.
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/{endpoint}"
        limit = offset + 1
        params = {"symbol": ticker, "period": period, "limit": limit, "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            if not data or len(data) <= offset:
                logger.warning(f"No {endpoint} data for {ticker} at offset {offset}")
                return {}
            return data[offset]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
            logger.error(f"Connection error fetching {endpoint} for {ticker}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching {endpoint} for {ticker}: {e}")
            return {}

    async def fetch_ratios(
        self, ticker: str, period: str = "annual", offset: int = 0
    ) -> dict[str, Any]:
        """Fetch financial ratios for a given period offset."""
        return await self._fetch_statement("ratios", ticker, period, offset)

    async def fetch_key_metrics(
        self, ticker: str, period: str = "annual", offset: int = 0
    ) -> dict[str, Any]:
        """Fetch key metrics for a given period offset."""
        return await self._fetch_statement("key-metrics", ticker, period, offset)

    async def fetch_balance_sheet(
        self, ticker: str, period: str = "annual", offset: int = 0
    ) -> dict[str, Any]:
        """Fetch balance sheet data for a given period offset."""
        return await self._fetch_statement("balance-sheet-statement", ticker, period, offset)

    async def fetch_income_statement(
        self, ticker: str, period: str = "annual", offset: int = 0
    ) -> dict[str, Any]:
        """Fetch income statement data for a given period offset."""
        return await self._fetch_statement("income-statement", ticker, period, offset)

    async def fetch_cash_flow(
        self, ticker: str, period: str = "annual", offset: int = 0
    ) -> dict[str, Any]:
        """Fetch cash flow data for a given period offset."""
        return await self._fetch_statement("cash-flow-statement", ticker, period, offset)

    async def _fetch_ttm(self, endpoint: str, ticker: str) -> dict[str, Any]:
        """Fetch trailing-twelve-months data from a TTM endpoint.

        TTM endpoints take only symbol (no period/limit) and return a
        single-element array.
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/{endpoint}"
        params = {"symbol": ticker, "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            if not data or len(data) == 0:
                logger.warning(f"No {endpoint} data for {ticker}")
                return {}
            return data[0]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
            logger.error(f"Connection error fetching {endpoint} for {ticker}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching {endpoint} for {ticker}: {e}")
            return {}

    async def fetch_ratios_ttm(self, ticker: str) -> dict[str, Any]:
        """Fetch trailing twelve months financial ratios."""
        return await self._fetch_ttm("ratios-ttm", ticker)

    async def fetch_key_metrics_ttm(self, ticker: str) -> dict[str, Any]:
        """Fetch trailing twelve months key metrics."""
        return await self._fetch_ttm("key-metrics-ttm", ticker)

    async def fetch_balance_sheet_ttm(self, ticker: str) -> dict[str, Any]:
        """Fetch trailing twelve months balance sheet."""
        return await self._fetch_ttm("balance-sheet-statement-ttm", ticker)

    async def fetch_income_statement_ttm(self, ticker: str) -> dict[str, Any]:
        """Fetch trailing twelve months income statement."""
        return await self._fetch_ttm("income-statement-ttm", ticker)

    async def fetch_cash_flow_ttm(self, ticker: str) -> dict[str, Any]:
        """Fetch trailing twelve months cash flow."""
        return await self._fetch_ttm("cash-flow-statement-ttm", ticker)

    async def fetch_profile(self, ticker: str) -> dict[str, Any]:
        """Fetch company profile data."""
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/profile"
        params = {"symbol": ticker, "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            if not data or len(data) == 0:
                logger.warning(f"No profile data for {ticker}")
                return {}
            return data[0]
        except Exception as e:
            logger.error(f"Error fetching profile for {ticker}: {e}")
            return {}

    async def fetch_treasury_rate(self, maturity_field: str) -> dict[str, Any]:
        """Fetch the most recent treasury rate for a given maturity.

        Args:
            maturity_field: FMP field name (e.g. "year10", "month3")

        Returns:
            Dict with "last_price" set to the yield value, matching the
            quote schema so the existing field mapping pipeline works.
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/treasury-rates"
        params = {"apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            if not data:
                return {}
            latest = data[0]
            rate = latest.get(maturity_field)
            if rate is None:
                logger.warning(f"No treasury rate for maturity {maturity_field}")
                return {}
            return {
                "last_price": rate,
                "open": rate,
                "high": rate,
                "low": rate,
            }
        except Exception as e:
            logger.error(f"Error fetching treasury rate: {e}")
            raise

    async def search_by_isin(self, isin: str) -> list[dict[str, Any]]:
        """Search for a company by ISIN identifier.

        Endpoint: GET /search-isin?isin={isin} (stable API)
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/search-isin"
        params = {"isin": isin, "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"ISIN search failed for {isin}: {e}")
            return []

    async def fetch_company_notes(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch company notes (corporate bonds/debt instruments).

        Endpoint: GET /company-notes?symbol={symbol} (stable API)
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/company-notes"
        params = {"symbol": symbol.upper(), "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"Company notes fetch failed for {symbol}: {e}")
            return []

    async def fetch_price_target_consensus(self, ticker: str) -> dict[str, Any]:
        """Fetch consensus analyst price target.

        Endpoint: GET /price-target-consensus?symbol={symbol} (stable API)
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/price-target-consensus"
        params = {"symbol": ticker, "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            if not data or len(data) == 0:
                logger.warning(f"No price target data for {ticker}")
                return {}
            return data[0]
        except Exception as e:
            logger.error(f"Error fetching price target consensus for {ticker}: {e}")
            return {}

    async def fetch_dividends_calendar(self, ticker: str) -> dict[str, Any]:
        """Fetch the most recent dividend event for a ticker.

        Endpoint: GET /dividends?symbol={symbol}&limit=1 (stable API)
        Uses the per-symbol Dividends Company endpoint (not the market-wide
        dividends-calendar which ignores the symbol parameter).
        Returns the most recent ex-dividend date and dividend amount.
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/dividends"
        params = {"symbol": ticker, "limit": 1, "apikey": self.api_key}

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()
            if not data or len(data) == 0:
                logger.warning(f"No dividend calendar data for {ticker}")
                return {}
            return data[0]
        except Exception as e:
            logger.error(f"Error fetching dividends calendar for {ticker}: {e}")
            return {}

    async def fetch_screener(
        self,
        sector: str | None = None,
        market_cap_min: float | None = None,
        market_cap_max: float | None = None,
        **kwargs,
    ) -> list[dict]:
        """Fetch screened securities with full company data.

        Endpoint: GET /company-screener (stable API)
        Returns full screening data including company name, market cap, price, etc.
        """
        if not self.api_key:
            raise ValueError("FMP API key not configured")

        url = f"{self.base_url}/company-screener"
        params: dict[str, Any] = {"apikey": self.api_key}

        # Add filters
        if sector:
            params["sector"] = sector
        if market_cap_min is not None:
            params["marketCapMoreThan"] = int(market_cap_min * 1_000_000)  # Convert to actual value
        if market_cap_max is not None:
            params["marketCapLowerThan"] = int(market_cap_max * 1_000_000)

        # Default limit
        params["limit"] = 100

        try:
            response = await self._get(url, params)
            response.raise_for_status()
            data = response.json()

            if not data:
                logger.warning("No securities found matching criteria")
                return []

            # Return full screening data (not just symbols)
            logger.info(f"Found {len(data)} securities matching criteria")
            return data

        except Exception as e:
            logger.error(f"Error fetching screener results: {e}")
            raise
