"""
OpenBB service with startup retry logic and health monitoring.
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openbb import obb

from openbb_app.models import (
    InitializationError,
    OpenBBServiceHealth,
    ProviderStatus,
    ServiceStatus,
)
from shared.config.openbb_config import OpenBBSettings, active_openbb_providers
from shared.utils.numerics import is_valid_value, to_float_safe, to_int_safe
from shared.utils.timestamp import extract_iso_timestamp

logger = logging.getLogger(__name__)


class OpenBBClient:
    """OpenBB client wrapper with health monitoring."""

    def __init__(self):
        self._client = None
        self._active_providers: dict[str, str] = {}
        self._provider_status: dict[str, ProviderStatus] = {}
        self._error: str | None = None
        self._status = ServiceStatus.INITIALIZING

    def initialize(
        self,
        fail_on_error: bool = False,
    ) -> bool:
        """
        Initialize OpenBB

        Returns True if successful, False if failed.
        Raises InitializationError if fail_on_error=True and initialization fails.
        """
        logger.info("Initializing OpenBB service...")

        try:
            # Setup environment
            data_dir = Path.home() / ".openbb_platform"
            os.environ["OPENBB_DATA_DIR"] = str(data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)

            # Load provider config
            settings = OpenBBSettings()
            self._active_providers = active_openbb_providers(settings)

            # Initialize provider status
            self._provider_status = {
                name: ProviderStatus(
                    name=name,
                    enabled=True,
                    has_credentials=cred.lower() != "free",
                )
                for name, cred in active_openbb_providers(settings).items()
            }

            logger.info(
                f"Loaded {len(self._active_providers)} providers: {list(self._active_providers.keys())}"
            )

            # Initialize OBB client
            self._client = obb

            self._status = ServiceStatus.HEALTHY
            logger.info("✅ Initialization successful")
            logger.info(f"✅ Providers: {len(self._active_providers)} configured")
            return True

        except Exception as e:
            self._error = str(e)
            self._status = ServiceStatus.FAILED
            logger.error("❌ Initialization failed")
            logger.error(self._error)

        if fail_on_error:
            raise InitializationError(
                message="Failed to initialize OpenBB service",
                error=self._error,
            )

        return False

    def get_health(self) -> OpenBBServiceHealth:
        """Get current health status."""
        return OpenBBServiceHealth(
            status=self._status,
            providers=list(self._provider_status.values()),
            error=self._error,
        )

    async def fetch_quote(self, ticker: str, provider: str | None = None) -> dict[str, Any]:
        """Fetch quote data for a ticker.

        Args:
            ticker: Stock ticker symbol
            provider: Specific provider to use (optional)

        Returns:
            Dictionary with quote data
        """
        if not self.is_healthy:
            raise RuntimeError("OpenBB client not healthy")

        ALLOWED_PROVIDERS = {"fmp", "intrinio", "yfinance"}

        if not provider:
            # Pick the first active provider that is in ALLOWED_PROVIDERS
            # Use list() to create a snapshot of keys for thread-safe iteration
            provider_candidates = [
                p for p in list(self._active_providers.keys()) if p in ALLOWED_PROVIDERS
            ]
            if not provider_candidates:
                raise RuntimeError("No active providers available from the allowed set")
            provider = provider_candidates[0]

        if provider not in ALLOWED_PROVIDERS:
            raise ValueError(
                f"Invalid provider '{provider}'. Must be one of: {', '.join(ALLOWED_PROVIDERS)}"
            )

        try:
            result = self._client.equity.price.quote(symbol=ticker, provider=provider)  # type: ignore

            if hasattr(result, "to_dict"):
                data: dict[str, Any] = result.to_dict()  # type: ignore
                return data
            elif result.results:
                data: dict[str, Any] = result.results[0].model_dump()
                return data
            else:
                return {}

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
        provider: str | None = None,
    ) -> pd.DataFrame:
        result: dict[str, Any] = {}

        ALLOWED_PROVIDERS = {"fmp", "intrinio", "yfinance"}

        if not provider:
            # Pick the first active provider that is in ALLOWED_PROVIDERS
            # Use list() to create a snapshot of keys for thread-safe iteration
            provider_candidates = [
                p for p in list(self._active_providers.keys()) if p in ALLOWED_PROVIDERS
            ]
            if not provider_candidates:
                raise RuntimeError("No active providers available from the allowed set")
            provider = provider_candidates[0]

        if provider not in ALLOWED_PROVIDERS:
            raise ValueError(
                f"Invalid provider '{provider}'. Must be one of: {', '.join(ALLOWED_PROVIDERS)}"
            )

        try:
            start = start_date.date()
            end = end_date.date()
            # Call the OpenBB client
            data = self._client.equity.price.historical(  # type: ignore
                symbol=symbol,
                start_date=start,
                end_date=end,
                interval=interval,
                provider=provider,  # type: ignore
                adjust_split=adjust_splits,
                adjust_dividend=adjust_dividends,
            )

            df = pd.DataFrame()
            if hasattr(data, "to_df"):
                df = data.to_df()
            elif isinstance(data, pd.DataFrame):
                df = data

            # Convert to dict records
            return df

        except Exception as e:
            result["historicalData"] = []
            result["error"] = f"Error fetching historical data for {symbol}: {e}"
            logger.warn(f"error: {e}")

        return pd.DataFrame()

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
        provider: str | None = None,
    ) -> dict[str, Any]:
        """
        Fetch intraday tick data for a given ticker over a datetime range.
        Supports optional inclusion flags and provider choice.
        Returns a dict with the results or error info.
        """
        try:
            # Synchronous call wrapped in to_thread
            df = await asyncio.to_thread(
                self._fetch_intraday_ticks_sync,
                ticker,
                event_types,
                start,
                end,
                include_condition_codes,
                include_exchange_codes,
                include_broker_codes,
                include_spread_price,
                include_yield,
                provider,
            )

            # Extract eidData if present in DataFrame attributes/metadata
            eid_data = []
            if hasattr(df, "attrs") and "eidData" in df.attrs:
                eid_data = df.attrs["eidData"]
            elif hasattr(df, "eidData"):
                eid_data = df.eidData

            # Transform DataFrame to tickData format
            tick_data = []
            if not df.empty:
                # Reset index to get date column if it's in the index
                if not isinstance(df.index, pd.RangeIndex):
                    df = df.reset_index()

                for _, row in df.iterrows():
                    time_str = extract_iso_timestamp(row, df)
                    if not time_str:
                        continue

                    spread_price_val = row.get("spread_price")
                    yield_val = row.get("yield")

                    tick_data.append(
                        {
                            "time": time_str,
                            "type": "TRADE",  # Default to TRADE type
                            "value": to_float_safe(
                                row.get("close", row.get("price", None)), default=0.0
                            ),
                            "size": to_int_safe(row.get("volume", None), default=0),
                            "conditionCodes": (
                                row.get("condition_code") if include_condition_codes else None
                            ),
                            "exchangeCode": (
                                row.get("exchange_code") if include_exchange_codes else None
                            ),
                            "brokerCode": row.get("broker_code") if include_broker_codes else None,
                            "spreadPrice": (
                                to_float_safe(spread_price_val)
                                if include_spread_price and is_valid_value(spread_price_val)
                                else None
                            ),
                            "yield": (
                                to_float_safe(yield_val)
                                if include_yield and is_valid_value(yield_val)
                                else None
                            ),
                        }
                    )

            return {"ticker": ticker, "eidData": eid_data, "tickData": tick_data, "error": None}
        except Exception as e:
            logger.error(f"Error fetching intraday ticks for {ticker}: {e}", exc_info=True)
            return {"ticker": ticker, "eidData": [], "tickData": [], "error": {"message": str(e)}}

    def _fetch_intraday_ticks_sync(
        self,
        ticker: str,
        event_types: list[str],
        start: datetime,
        end: datetime,
        include_condition_codes: bool,
        include_exchange_codes: bool,
        include_broker_codes: bool,
        include_spread_price: bool,
        include_yield: bool,
        provider: str | None = None,
    ) -> pd.DataFrame:
        """
        Sync helper that actually calls the OpenBB client (or whichever provider)
        and returns raw result (DataFrame or list/dict).
        """

        ALLOWED_PROVIDERS = {"fmp", "intrinio", "polygon", "tiingo", "yfinance"}

        if not provider:
            # Pick the first active provider that is in ALLOWED_PROVIDERS
            # Use list() to create a snapshot of keys for thread-safe iteration
            provider_candidates = [
                p for p in list(self._active_providers.keys()) if p in ALLOWED_PROVIDERS
            ]
            if not provider_candidates:
                raise RuntimeError("No active providers available from the allowed set")
            provider = provider_candidates[0]

        if provider not in ALLOWED_PROVIDERS:
            raise ValueError(
                f"Invalid provider '{provider}'. Must be one of: {', '.join(ALLOWED_PROVIDERS)}"
            )

        logger.info(
            f"Fetching intraday ticks for {ticker} from {provider}, "
            f"start={start.isoformat()}, end={end.isoformat()}, events={event_types}"
        )

        # Note: Adjust this call depending on what method your client supports
        data = self._client.equity.price.historical(  # type: ignore
            symbol=ticker,
            start_date=start.date().isoformat(),
            end_date=end.date().isoformat(),
            interval="1m",  # or "tick" if available
            provider=provider,  # type: ignore
        )

        df = pd.DataFrame()
        if hasattr(data, "to_df"):
            df = data.to_df()
        elif isinstance(data, pd.DataFrame):
            df = data
        else:
            logger.warning(f"Unexpected data type for intraday ticks from {provider}: {type(data)}")

        # Add optional fields
        if not include_condition_codes:
            df = df.drop(columns=["condition_code"], errors="ignore")
        if not include_exchange_codes:
            df = df.drop(columns=["exchange_code"], errors="ignore")
        if not include_broker_codes:
            df = df.drop(columns=["broker_code"], errors="ignore")
        if not include_spread_price:
            df = df.drop(columns=["spread_price"], errors="ignore")
        if not include_yield:
            df = df.drop(columns=["yield"], errors="ignore")

        logger.info(f"Fetched {len(df)} intraday ticks for {ticker} from {provider}")
        if df.empty:
            logger.warning(f"No intraday tick data returned for {ticker} from {provider}")

        return df

    async def fetch_intraday_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch intraday bar data (OHLCV) from OpenBB."""
        try:
            result = await asyncio.to_thread(
                self._fetch_intraday_bars_sync, ticker, interval, start, end
            )
            return result
        except Exception as e:
            logger.error(f"Error fetching intraday bars for {ticker}: {e}", exc_info=True)
            return pd.DataFrame(
                columns=pd.Index(["timestamp", "open", "high", "low", "close", "volume"])
            )

    def _fetch_intraday_bars_sync(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Sync wrapper that calls OpenBB and returns a DataFrame."""

        ALLOWED_PROVIDERS = {"fmp", "intrinio", "polygon", "tiingo", "yfinance"}

        # Auto-select the first active provider from allowed list
        # Use list() to create a snapshot of keys for thread-safe iteration
        provider_candidates = [
            p for p in list(self._active_providers.keys()) if p in ALLOWED_PROVIDERS
        ]
        if not provider_candidates:
            raise RuntimeError("No active providers available from the allowed set")
        provider = provider_candidates[0]

        start_date = start.date()
        end_date = end.date()

        data = self.client.equity.price.historical(  # or equity.historical.intraday, depending on version # type: ignore
            symbol=ticker,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            provider=provider,  # type: ignore
        )

        if hasattr(data, "to_df"):
            return data.to_df()
        elif isinstance(data, pd.DataFrame):
            return data
        else:
            return pd.DataFrame()

    @property
    def client(self):
        """Get the OBB client."""
        if not self._status == ServiceStatus.HEALTHY:
            raise RuntimeError("OpenBB client not healthy")
        return self._client

    @property
    def active_providers(self) -> dict[str, str]:
        """Get active providers."""
        if not self._status == ServiceStatus.HEALTHY:
            raise RuntimeError("OpenBB client not healthy")
        return self._active_providers

    @property
    def is_healthy(self) -> bool:
        """Check if service is usable."""
        return self._status in [ServiceStatus.HEALTHY]

    @property
    def status(self) -> ServiceStatus:
        """Get current status."""
        return self._status

    def get_working_providers(self) -> list[str]:
        """Get list of configured provider names."""
        return list(self._provider_status.keys())

    def is_provider_available(self, provider_name: str) -> bool:
        """Check if a specific provider is configured."""
        return provider_name in self._provider_status


# Global instance
_openbb_client_instance: OpenBBClient | None = None


def get_openbb_client() -> OpenBBClient:
    """Get the global OpenBB client instance."""
    global _openbb_client_instance
    if _openbb_client_instance is None:
        raise RuntimeError("OpenBB client not initialized")
    return _openbb_client_instance


def initialize_openbb_client(
    fail_on_error: bool = False,
) -> OpenBBClient:
    """Initialize the global OpenBB client instance."""
    global _openbb_client_instance

    if _openbb_client_instance is not None:
        return _openbb_client_instance

    _openbb_client_instance = OpenBBClient()
    _openbb_client_instance.initialize(
        fail_on_error=fail_on_error,
    )

    return _openbb_client_instance
