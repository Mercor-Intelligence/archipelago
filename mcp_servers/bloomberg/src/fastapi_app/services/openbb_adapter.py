"""
OpenBB Adapter for Bloomberg-Compatible Data Translation

This module provides translation between Bloomberg and OpenBB/provider data formats.
Handles field mapping, security identifier parsing, and data transformation.
Does NOT perform network calls - uses OpenBBClient for data fetching.
"""

import logging
import re
from datetime import datetime
from typing import Any

import pandas as pd

from fastapi_app.clients.base_client import BloombergClient
from fastapi_app.models.beqs import BeqsRequest
from fastapi_app.models.enums import PeriodicitySelection
from fastapi_app.models.responses import ReferenceDataRequest
from fastapi_app.services.field_calculators import calculate_field
from openbb_app.openbb_client import OpenBBClient as OpenBBService
from openbb_app.providers.equity import EquityProvider
from openbb_app.translators.beqs_translator import BeqsTranslator
from shared.models.error_models import classify_and_create_error
from shared.models.fields import FIELDS

logger = logging.getLogger(__name__)

# Create field lookup dict once at module level for efficient access
# Map: (mnemonic, request_type) -> FieldInfo
FIELDS_LOOKUP = {}
for f in FIELDS:
    for req_type in f.request_types:
        FIELDS_LOOKUP[(f.mnemonic, req_type)] = f

# Bloomberg to provider field mapping for reference data
BLOOMBERG_TO_PROVIDER = {
    "PX_LAST": "last_price",
    "BID": "bid",
    "ASK": "ask",
    "BID_SIZE": "bid_size",
    "ASK_SIZE": "ask_size",
    "PX_OPEN": "open",
    "PX_HIGH": "high",
    "PX_LOW": "low",
    "VOLUME": "volume",
}

# ---------------------------------------------------------------------------
# Non-equity identifier mapping tables
# ---------------------------------------------------------------------------

INDEX_MAP: dict[str, str] = {
    "SPX": "^GSPC",
    "INDU": "^DJI",
    "CCMP": "^IXIC",
    "NDX": "^NDX",
    "RTY": "^RUT",
    "RUT": "^RUT",
    "UKX": "^FTSE",
    "NKY": "^N225",
    "DAX": "^GDAXI",
    "CAC": "^FCHI",
    "HSI": "^HSI",
    "SHCOMP": "000001.SS",
    "SX5E": "^STOXX50E",
    "SXXP": "^STOXX",
    "AS51": "^AXJO",
    "SENSEX": "^BSESN",
    "NIFTY": "^NSEI",
    "KOSPI": "^KS11",
    "IBOV": "^BVSP",
    "MEXBOL": "^MXX",
    "SPTSX": "^GSPTSE",
    "VIX": "^VIX",
    "MOVE": "^MOVE",
    "MXWO": "MSCIWORLD",
    "MID": "^MID",
    "NYA": "^NYA",
    "W5000": "^W5000",
    "IBEX": "^IBEX",
    "SMI": "^SSMI",
    "STI": "^STI",
    "TWSE": "^TWII",
    "SET": "^SET.BK",
    "FBMKLCI": "^KLSE",
    "JCI": "^JKSE",
    "NZX50": "^NZ50",
    "OMX": "^OMXS30",
    "AEX": "^AEX",
    "BEL20": "^BFX",
    "TASI": "^TASI.SR",
    "TA125": "^TA125.TA",
}

COMMODITY_MAP: dict[str, str] = {
    "GC1": "GCUSD",
    "SI1": "SIUSD",
    "CL1": "CLUSD",
    "CO1": "BZUSD",
    "NG1": "NGUSD",
    "HG1": "HGUSD",
    "PL1": "PLUSD",
    "PA1": "PAUSD",
    "W": "KEUSX",
    "C": "ZCUSX",
    "S": "ZSUSX",
    "KC1": "KCUSX",
    "SB1": "SBUSX",
    "CT1": "CTUSX",
    "HO1": "HOUSD",
    "XB1": "RBUSD",
    "LA1": "ALIUSD",
    "LB1": "LBUSD",
    "LC1": "LEUSX",
    "LH1": "HEUSX",
    "OJ1": "OJUSX",
    "CC1": "CCUSD",
}

CURRENCY_MAP: dict[str, str] = {
    "EUR": "EURUSD",
    "GBP": "GBPUSD",
    "AUD": "AUDUSD",
    "NZD": "NZDUSD",
    "JPY": "USDJPY",
    "CHF": "USDCHF",
    "CAD": "USDCAD",
    "NOK": "USDNOK",
    "SEK": "USDSEK",
    "DKK": "USDDKK",
    "MXN": "USDMXN",
    "ZAR": "USDZAR",
    "SGD": "USDSGD",
    "HKD": "USDHKD",
    "CNY": "USDCNY",
    "CNH": "USDCNH",
    "INR": "USDINR",
    "BRL": "USDBRL",
    "TRY": "USDTRY",
    "KRW": "USDKRW",
    "PLN": "USDPLN",
    "THB": "USDTHB",
    "TWD": "USDTWD",
    "ILS": "USDILS",
    "CLP": "USDCLP",
    "COP": "USDCOP",
    "PHP": "USDPHP",
    "IDR": "USDIDR",
    "MYR": "USDMYR",
}

TREASURY_MATURITY_MAP: dict[str, str] = {
    "USGG1M": "month1",
    "USGG2M": "month2",
    "USGG3M": "month3",
    "USGG6M": "month6",
    "USGG1YR": "year1",
    "USGG12M": "year1",
    "USGG2YR": "year2",
    "USGG3YR": "year3",
    "USGG5YR": "year5",
    "USGG7YR": "year7",
    "USGG10YR": "year10",
    "USGG20YR": "year20",
    "USGG30YR": "year30",
    "GB3M": "month3",
    "GB6M": "month6",
    "GB12M": "year1",
}


SHARE_CLASS_SUFFIXES = frozenset({".ST", ".CO", ".OL", ".HE"})


class OpenBBAdapter:
    """
    Adapter for translating between Bloomberg and provider data formats.

    Features:
    - Field mapping from Bloomberg to OpenBB/yfinance
    - Security identifier parsing (Bloomberg -> Yahoo Finance)
    - Data transformation and normalization
    - Override validation

    Does NOT perform network calls - uses OpenBBClient for data fetching.
    """

    def __init__(self, client: BloombergClient):
        """Initialize adapter with a Bloomberg-compatible client.

        Args:
            client: Any client implementing the BloombergClient interface
                   (OpenBBClient, FMPClient, MockOpenBBClient, OfflineClient, etc.)
        """
        self.client = client
        self.equity_provider = None
        self.beqs_translator = None

        if isinstance(client, OpenBBService):
            self.equity_provider = EquityProvider(client)
            self.beqs_translator = BeqsTranslator(self.equity_provider)

    @staticmethod
    def reference_data_request(security: str) -> tuple[str, str, str]:
        """
        Parse Bloomberg security identifier into symbol, exchange code,
        and instrument type.

        Args:
            security: Bloomberg format (e.g., "AAPL US Equity", "SPX Index",
                      "GC1 Comdty", "EUR Curncy", "USGG10YR Index")

        Returns:
            Tuple of (symbol, exchange_code, instrument_type) where:
            - symbol: FMP-compatible ticker
            - exchange_code: Bloomberg exchange code (or instrument class)
            - instrument_type: one of "Equity", "Index", "Comdty", "Curncy",
              "Treasury"

        Examples:
            "AAPL US Equity"   -> ("AAPL", "US", "Equity")
            "VOD LN Equity"    -> ("VOD.L", "LN", "Equity")
            "SPX Index"        -> ("^GSPC", "Index", "Index")
            "GC1 Comdty"       -> ("GCUSD", "Comdty", "Comdty")
            "EUR Curncy"       -> ("EURUSD", "Curncy", "Curncy")
            "USGG10YR Index"   -> ("USGG10YR", "Index", "Treasury")
        """
        parts = security.split()
        if len(parts) < 2:
            raise ValueError(f"Invalid security format: {security}")

        sym = parts[0]
        instrument_class = parts[-1]

        # --- Non-equity instruments ---

        if instrument_class == "Index":
            if sym in TREASURY_MATURITY_MAP:
                return sym, "Index", "Treasury"
            fmp_sym = INDEX_MAP.get(sym, sym)
            return fmp_sym, "Index", "Index"

        if instrument_class == "Comdty":
            fmp_sym = COMMODITY_MAP.get(sym)
            if fmp_sym is None:
                root = re.sub(r"\d+$", "", sym)
                fmp_sym = COMMODITY_MAP.get(root, f"{root}USD")
            return fmp_sym, "Comdty", "Comdty"

        if instrument_class == "Curncy":
            fmp_sym = CURRENCY_MAP.get(sym, f"{sym}USD")
            return fmp_sym, "Curncy", "Curncy"

        if instrument_class in ("Corp", "Govt", "Mtge", "Muni"):
            return sym, instrument_class, "Bond"

        # --- Equity (default) ---

        exchange_code = parts[1] if len(parts) >= 2 else "US"

        # Map Bloomberg exchange codes to FMP/Yahoo Finance suffixes
        exchange_suffix_map = {
            "US": "",  # US stocks don't need suffix
            "LN": ".L",  # London Stock Exchange
            "JP": ".T",  # Tokyo Stock Exchange
            "HK": ".HK",  # Hong Kong Stock Exchange
            "GR": ".DE",  # Deutsche Börse (Frankfurt/XETRA)
            "FP": ".PA",  # Euronext Paris
            "AU": ".AX",  # Australian Securities Exchange
            "CN": ".TO",  # Toronto Stock Exchange
            "IT": ".MI",  # Borsa Italiana (Milan)
            "SM": ".MC",  # Bolsa de Madrid
            "SW": ".SW",  # SIX Swiss Exchange
            "SS": ".ST",  # Nasdaq Stockholm
            "DC": ".CO",  # Nasdaq Copenhagen (Denmark)
            "SJ": ".JO",  # Johannesburg Stock Exchange
            "IN": ".NS",  # National Stock Exchange of India
            "KS": ".KS",  # Korea Stock Exchange
            "SP": ".SI",  # Singapore Exchange
            "TB": ".BK",  # Stock Exchange of Thailand
            "NZ": ".NZ",  # New Zealand Exchange
            "BZ": ".SA",  # B3 / São Paulo (Brazil)
            "NO": ".OL",  # Oslo Børs (Norway)
            "FH": ".HE",  # Nasdaq Helsinki (Finland)
        }

        suffix = exchange_suffix_map.get(exchange_code, "")
        symbol = f"{sym}{suffix}"

        return symbol, exchange_code, "Equity"

    @staticmethod
    def _share_class_variant(symbol: str) -> str | None:
        """Generate a hyphenated share-class ticker variant for Nordic exchanges.

        Bloomberg concatenates the share class letter (e.g. ERICB for Ericsson B),
        while FMP/Yahoo separates it with a hyphen (ERIC-B). This method returns
        the hyphenated variant for Nordic exchanges where share classes are common,
        or None if not applicable.
        """
        if "." not in symbol:
            return None
        base, suffix = symbol.rsplit(".", 1)
        if f".{suffix}" not in SHARE_CLASS_SUFFIXES:
            return None
        if len(base) < 2:
            return None
        last_char = base[-1]
        if last_char.upper() not in ("A", "B", "C"):
            return None
        return f"{base[:-1]}-{last_char}.{suffix}"

    @staticmethod
    def map_periodicity(periodicity: PeriodicitySelection) -> str:
        """
        Map Bloomberg periodicity to OpenBB/yfinance interval.

        Args:
            periodicity: Bloomberg periodicity

        Returns:
            OpenBB interval string
        """
        periodicity_map = {
            PeriodicitySelection.DAILY: "1d",
            PeriodicitySelection.WEEKLY: "1wk",
            PeriodicitySelection.MONTHLY: "1mo",
            PeriodicitySelection.QUARTERLY: "3mo",
            PeriodicitySelection.YEARLY: "1y",
        }
        return periodicity_map.get(periodicity, "1d")

    async def fetch_quote(self, ticker: str) -> dict[str, Any]:
        """
        Fetch quote data for a ticker using the OpenBB client.

        This is a convenience method that delegates to the client.

        Args:
            ticker: Stock ticker symbol

        Returns:
            Dictionary with provider-normalized field names
        """
        result = await self.client.fetch_quote(ticker)
        if not result:
            variant = self._share_class_variant(ticker)
            if variant:
                logger.info(f"Retrying quote with share-class variant: {ticker} -> {variant}")
                result = await self.client.fetch_quote(variant)
        return result

    @staticmethod
    def map_fields(quote_data: dict[str, Any], requested_fields: list[str]) -> dict[str, Any]:
        """
        Map provider-normalized data to Bloomberg field format.

        Args:
            quote_data: Raw data from provider with normalized field names
            requested_fields: List of requested Bloomberg fields

        Returns:
            Dictionary with Bloomberg field names
        """
        result = {}

        for bloomberg_field in requested_fields:
            if bloomberg_field not in BLOOMBERG_TO_PROVIDER:
                logger.debug(f"Unknown Bloomberg field: {bloomberg_field}")
                continue

            provider_field = BLOOMBERG_TO_PROVIDER[bloomberg_field]
            value = quote_data.get(provider_field)

            if value is not None:
                result[bloomberg_field] = value

        return result

    async def fetch_historical_data(
        self,
        security: str,
        fields: list[str],
        start_date: datetime,
        end_date: datetime,
        periodicity: PeriodicitySelection = PeriodicitySelection.DAILY,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Fetch and translate historical data for a single security.

        Args:
            security: Bloomberg security identifier
            fields: List of Bloomberg field mnemonics
            start_date: Start date
            end_date: End date
            periodicity: Data periodicity
            **kwargs: Additional parameters (adjustments, etc.)

        Returns:
            Tuple of (DataFrame with Bloomberg fields, error dict if failed)
        """
        # Parse security identifier
        symbol, *_ = self.reference_data_request(security)

        # Map periodicity
        interval = self.map_periodicity(periodicity)

        # Extract adjustment parameters
        adjust_splits = kwargs.get("adjustmentSplit", True)
        adjust_dividends = kwargs.get("adjustmentNormal", True) or kwargs.get(
            "adjustmentAbnormal", False
        )

        logger.info(
            f"Fetching {symbol} with adjustments: "
            f"splits={adjust_splits}, dividends={adjust_dividends}"
        )

        fetch_kwargs = {
            "start_date": start_date,
            "end_date": end_date,
            "interval": interval,
            "adjust_splits": adjust_splits,
            "adjust_dividends": adjust_dividends,
        }

        # Fetch data using client
        raw_data = await self.client.fetch_historical_data(symbol=symbol, **fetch_kwargs)

        if raw_data.empty:
            variant = self._share_class_variant(symbol)
            if variant:
                logger.info(f"Retrying historical with share-class variant: {symbol} -> {variant}")
                raw_data = await self.client.fetch_historical_data(symbol=variant, **fetch_kwargs)

        # Parse fields to Bloomberg format
        result = self.parse_fields(raw_data, fields, "HistoricalDataRequest")

        return result

    @staticmethod
    def _normalize_dataframe(data: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize DataFrame columns and ensure date column exists.

        Args:
            data: Raw DataFrame from provider

        Returns:
            Normalized DataFrame with lowercase columns and date column
        """
        # Normalize column names to lowercase (preserve spaces to avoid duplicates)
        # yfinance returns capitalized columns (Open, High, Low, Close, Volume, "Adj Close")
        data = data.copy()
        # Ensure columns are strings before using .str accessor
        data.columns = pd.Index([str(c).lower() for c in data.columns])

        # Handle "adj close" specifically: rename to "adjclose" only if needed
        # bugbot-ignore: duplicate-columns
        # Reason: Only one data provider is called per request. Providers never return
        # both "Adj Close" and "AdjClose" in the same DataFrame.
        # This avoids creating duplicate columns if both "Adj Close" and "AdjClose" exist
        if "adjclose" not in data.columns and "adj close" in data.columns:
            data = data.rename(columns={"adj close": "adjclose"})

        # Ensure date column exists
        # If date column is missing, check if the index contains date/datetime data
        if "date" not in data.columns:
            # Check if index has a name (e.g., "Date", "Datetime", "date")
            # If so, reset_index will create a column from it
            if data.index.name and isinstance(data.index.name, str):
                # Reset index to convert it to a column
                data = data.reset_index()
                # Normalize column names again after reset_index (lowercase only)
                data.columns = pd.Index([str(c).lower() for c in data.columns])
                # Handle "adj close" specifically
                if "adjclose" not in data.columns and "adj close" in data.columns:
                    data = data.rename(columns={"adj close": "adjclose"})
            # If index has no name but appears to be a datetime index, create date column
            elif hasattr(data.index, "date") or pd.api.types.is_datetime64_any_dtype(data.index):
                data = data.reset_index()
                data.columns = pd.Index([str(c).lower() for c in data.columns])
                # Handle "adj close" specifically
                if "adjclose" not in data.columns and "adj close" in data.columns:
                    data = data.rename(columns={"adj close": "adjclose"})
                # Rename the index column to 'date' if it's not already named
                if "date" not in data.columns and len(data.columns) > 0:
                    # Find the likely date column (first column after reset_index)
                    date_col_candidates = [
                        col for col in data.columns if col in ["index", "datetime", "timestamp"]
                    ]
                    if date_col_candidates:
                        data = data.rename(columns={date_col_candidates[0]: "date"})

        return data

    @staticmethod
    def _get_field_value(field: str, field_def: Any, data: pd.DataFrame) -> pd.Series | None:
        """
        Get field value from data using mapping or calculation.

        Args:
            field: Bloomberg field mnemonic
            field_def: Field definition from registry
            data: Normalized DataFrame with lowercase columns

        Returns:
            Series with field values or None
        """

        # Direct mapping to provider field
        if field_def.openbb_mapping:
            openbb_field = field_def.openbb_mapping
            if openbb_field in data.columns:
                return pd.Series(data[openbb_field])
            return None

        # Calculated field using field calculator
        return calculate_field(field, data)

    @staticmethod
    def parse_fields(
        data: pd.DataFrame, requested_fields: list[str], request_type: str
    ) -> pd.DataFrame:
        """
        Parse and map provider data to Bloomberg field format.

        This method handles both direct field mappings (e.g., PX_LAST -> close)
        and calculated fields (e.g., TURNOVER, VWAP).

        Args:
            data: Raw DataFrame from provider with provider field names
            requested_fields: List of requested Bloomberg field mnemonics
            request_type: Type of request (HistoricalDataRequest, ReferenceDataRequest, etc.)

        Returns:
            DataFrame with only the date column and requested Bloomberg fields
        """
        # Normalize DataFrame (lowercase columns, ensure date column exists)
        normalized_data = OpenBBAdapter._normalize_dataframe(data)

        # Create result DataFrame starting with date column
        result = pd.DataFrame()
        if "date" in normalized_data.columns:
            result["date"] = normalized_data["date"]

        # Process each requested field
        for field in requested_fields:
            # Get field info from pre-loaded FIELDS_LOOKUP using (mnemonic, request_type)
            field_info = FIELDS_LOOKUP.get((field, request_type))

            if field_info is None:
                # Unknown field - log and skip
                logger.debug(f"Unknown Bloomberg field: {field} for {request_type}")
                result[field] = None
                continue

            # Check if it's a direct mapping
            if field_info.openbb_mapping:
                provider_field = field_info.openbb_mapping
                if provider_field in normalized_data.columns:
                    result[field] = normalized_data[provider_field]
                else:
                    logger.debug(f"Provider field {provider_field} not available for {field}")
                    result[field] = None
            else:
                # Try calculated field using field_calculators
                calculated_value = calculate_field(field, normalized_data)
                if calculated_value is not None:
                    result[field] = calculated_value
                else:
                    logger.debug(f"Field {field} has no mapping or calculation logic")
                    result[field] = None

        return result

    async def fetch_multiple_securities(
        self,
        securities: list[str],
        fields: list[str],
        start_date: datetime,
        end_date: datetime,
        periodicity: PeriodicitySelection = PeriodicitySelection.DAILY,
        **kwargs,
    ) -> list[tuple[str, pd.DataFrame | None, dict[str, Any] | None]]:
        """
        Fetch and translate historical data for multiple securities concurrently.

        Args:
            securities: List of Bloomberg security identifiers
            fields: List of Bloomberg field mnemonics
            start_date: Start date
            end_date: End date
            periodicity: Data periodicity
            **kwargs: Additional parameters

        Returns:
            List of tuples (security, DataFrame, error) for each security
        """
        import asyncio

        tasks = [
            self.fetch_historical_data(
                security, fields, start_date, end_date, periodicity, **kwargs
            )
            for security in securities
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Combine results with security identifiers
        output = []
        for security, result in zip(securities, results, strict=True):
            if isinstance(result, BaseException):
                error = classify_and_create_error(security=security, exc=result)
                output.append((security, None, error.model_dump()))
            else:
                # Unpack result if it is a tuple (DataFrame | None, dict | None)
                if isinstance(result, tuple) and len(result) == 2:
                    df, err_dict = result
                else:
                    df, err_dict = result, None

                output.append((security, df, err_dict))

        return output

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
        """Fetch intraday tick data (uses 1-minute bars as ticks, like v1-gui)."""
        # Parse Bloomberg security format (e.g., "AAPL US Equity" -> "AAPL")
        symbol, *_ = self.reference_data_request(ticker)

        logger.info(f"Fetching intraday ticks for {ticker} (parsed: {symbol})")

        tick_kwargs = {
            "event_types": event_types,
            "start": start,
            "end": end,
            "include_condition_codes": include_condition_codes,
            "include_exchange_codes": include_exchange_codes,
            "include_broker_codes": include_broker_codes,
            "include_spread_price": include_spread_price,
            "include_yield": include_yield,
        }

        result = await self.client.fetch_intraday_ticks(ticker=symbol, **tick_kwargs)

        if not result.get("tickData"):
            variant = self._share_class_variant(symbol)
            if variant:
                logger.info(f"Retrying ticks with share-class variant: {symbol} -> {variant}")
                result = await self.client.fetch_intraday_ticks(ticker=variant, **tick_kwargs)

        return result

    async def fetch_intraday_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        # Parse Bloomberg security format (e.g., "AAPL US Equity" -> "AAPL")
        symbol, *_ = self.reference_data_request(ticker)

        logger.info(f"Fetching intraday bars for {ticker} (parsed: {symbol})")

        result = await self.client.fetch_intraday_bars(
            ticker=symbol, interval=interval, start=start, end=end
        )

        if result.empty:
            variant = self._share_class_variant(symbol)
            if variant:
                logger.info(f"Retrying bars with share-class variant: {symbol} -> {variant}")
                result = await self.client.fetch_intraday_bars(
                    ticker=variant, interval=interval, start=start, end=end
                )

        return result

    async def execute_beqs_screen(self, request: BeqsRequest) -> dict[str, Any]:
        """Execute BEQS screen and return dict response."""
        # Check if client supports screening (duck typing for FMPClient or compatible clients)
        if hasattr(self.client, "fetch_screener"):
            logger.info("execute_beqs_screen with screening-capable client")
            try:
                # Extract filter parameters from request
                # Handle overrides as either dict (from JSON) or BeqsOverrides dataclass
                overrides = request.overrides
                if isinstance(overrides, dict):
                    sector = overrides.get("sector")
                    market_cap_min = overrides.get("marketCapMin")
                    market_cap_max = overrides.get("marketCapMax")
                elif overrides:
                    sector = overrides.sector  # type: ignore[union-attr]
                    market_cap_min = overrides.marketCapMin  # type: ignore[union-attr]
                    market_cap_max = overrides.marketCapMax  # type: ignore[union-attr]
                else:
                    sector = None
                    market_cap_min = None
                    market_cap_max = None

                # Convert enum to value if needed (sector could be Sector enum or string)
                from fastapi_app.models.enums import Sector

                if isinstance(sector, Sector):
                    sector = sector.value

                # Call FMP screener (returns full screening data)
                screening_data = await self.client.fetch_screener(  # type: ignore[attr-defined]
                    sector=sector, market_cap_min=market_cap_min, market_cap_max=market_cap_max
                )

                # Format response in Bloomberg BEQS format with rich data in customFields
                from datetime import datetime

                securities_list = []
                for item in screening_data[:50]:  # Limit to 50 results
                    symbol = item.get("symbol", "")
                    securities_list.append(
                        {
                            # Bloomberg standard fields
                            "security": f"{symbol} US Equity",
                            "ticker": symbol,
                            "name": item.get("companyName", symbol),
                            "exchange": item.get("exchange", "Unknown"),
                            "marketSector": item.get("sector", "Unknown"),
                            "industry": item.get("industry", "Unknown"),
                            # Rich FMP data in customFields (accessible by UI)
                            "customFields": {
                                "symbol": symbol,
                                "companyName": item.get("companyName", symbol),
                                "marketCap": item.get("marketCap"),
                                "sector": item.get("sector", "Unknown"),
                                "price": item.get("price"),
                                "volume": item.get("volume"),
                                "beta": item.get("beta"),
                                "lastAnnualDividend": item.get("lastAnnualDividend"),
                                "exchangeShortName": item.get("exchangeShortName", ""),
                                "country": item.get("country", ""),
                                "isEtf": item.get("isEtf", False),
                                "isFund": item.get("isFund", False),
                                "isActivelyTrading": item.get("isActivelyTrading", True),
                            },
                        }
                    )

                return {
                    "responseType": "BeqsResponse",
                    "screenName": request.screenName,
                    "screenType": request.screenType,
                    "asOfDate": datetime.now().isoformat(),
                    "totalSecurities": len(securities_list),
                    "securities": securities_list,
                    "responseErrors": [],
                }
            except Exception as e:
                logger.error(f"FMP screener error: {e}")
                return {
                    "responseType": "BeqsResponse",
                    "screenName": request.screenName,
                    "screenType": request.screenType,
                    "asOfDate": "",
                    "totalSecurities": 0,
                    "securities": [],
                    "responseErrors": [
                        {
                            "security": request.screenName,
                            "errorCode": "SCREEN_EXECUTION_ERROR",
                            "message": str(e),
                        }
                    ],
                }

        # Fall back to OpenBBService BEQS translator
        if self.beqs_translator is None:
            raise RuntimeError(
                "BEQS functionality requires OpenBBService client or FMPClient. "
                "Current client does not support BEQS operations."
            )

        logger.info("execute_beqs_screen with OpenBBService")
        response = await self.beqs_translator.execute(request)
        return response.to_dict()

    @staticmethod
    def _parse_period_overrides(
        overrides: list[Any] | None,
    ) -> tuple[str, int]:
        """Parse Bloomberg period overrides into FMP period and offset.

        Supports:
          - EQY_FUND_RELATIVE_PERIOD "-1Q"/"-2Y" → quarterly/annual offset
          - FUND_PER_CD "LTM"/"TTM" → trailing twelve months
          - FUND_PER_CD "Q"/"QTR"/"QUARTERLY" → most recent quarter

        Returns:
            (period, offset) where period is "annual", "quarter", or "ttm"
            and offset is 0 for most recent, 1 for one period back, etc.
        """
        if not overrides:
            return "annual", 0

        for ov in overrides:
            field_id = getattr(ov, "fieldId", None) or (
                ov.get("fieldId") if isinstance(ov, dict) else None
            )
            value = getattr(ov, "value", None) or (
                ov.get("value") if isinstance(ov, dict) else None
            )

            if field_id == "EQY_FUND_RELATIVE_PERIOD" and value:
                m = re.match(r"^-?(\d+)([QY])$", value.strip())
                if m:
                    offset = int(m.group(1))
                    period = "quarter" if m.group(2) == "Q" else "annual"
                    return period, offset

            if field_id == "FUND_PER_CD" and value:
                upper = value.strip().upper()
                if upper in ("LTM", "TTM"):
                    return "ttm", 0
                if upper in ("Q", "QTR", "QUARTERLY"):
                    return "quarter", 0

        return "annual", 0

    async def _fetch_bond_data(self, isin: str, requested_fields: list[str]) -> dict[str, Any]:
        """Look up bond data via FMP: ISIN search → company notes.

        Steps:
        1. search_by_isin() to find the issuing company's ticker
        2. fetch_company_notes() to get all notes/bonds for that company
        3. Match by ISIN and map Bloomberg fields to note data
        """
        if not hasattr(self.client, "search_by_isin"):
            logger.info(f"Client does not support ISIN search for bond {isin}")
            return {}

        # Step 1: Find company by ISIN
        isin_results = await self.client.search_by_isin(isin)  # type: ignore[attr-defined]
        if not isin_results:
            logger.info(f"No company found for ISIN {isin}")
            return {}

        symbol = isin_results[0].get("symbol")
        if not symbol:
            return {}

        # Step 2: Get company notes (bonds/debt)
        if not hasattr(self.client, "fetch_company_notes"):
            return {}

        notes = await self.client.fetch_company_notes(symbol)  # type: ignore[attr-defined]
        if not notes:
            logger.info(f"No notes found for {symbol}")
            return {}

        # Step 3: Find the matching note by ISIN (if available) or use first
        matched_note = None
        for note in notes:
            if note.get("isin") == isin or note.get("cusip") == isin:
                matched_note = note
                break
        if not matched_note:
            matched_note = notes[0]

        # Step 4: Map FMP company-notes fields to Bloomberg fields
        field_map = {
            "NAME": matched_note.get("title", ""),
            "CPN": matched_note.get("couponRate"),
            "MATURITY": matched_note.get("maturityDate", ""),
            "MATURITY_DATE_ISSUE_EXCH": matched_note.get("maturityDate", ""),
            "MATURITY_DATE_ISSUE_PX": matched_note.get("maturityDate", ""),
            "ISSUE_DT": matched_note.get("offeringDate", ""),
            "CURRENCY": matched_note.get("currency", "USD"),
            "AMT_OUTSTANDING": matched_note.get("totalDebt"),
            "AMT_ISSUED": matched_note.get("totalDebt"),
            "PX_LAST": matched_note.get("lastPrice"),
            "COUNTRY": matched_note.get("country", ""),
            "TICKER": symbol,
            "EXCH_CODE": matched_note.get("exchange", ""),
        }

        result: dict[str, Any] = {}
        for field in requested_fields:
            if field in field_map and field_map[field] is not None:
                result[field] = field_map[field]

        return result

    async def fetch_reference_data(
        self,
        ticker: str,
        requested_fields: list[str],
        overrides: list[Any] | None = None,
        instrument_type: str = "Equity",
    ) -> dict[str, Any]:
        """Fetch reference data for a ticker, routing fields to appropriate FMP endpoints.

        Handles equities (quote + fundamental data), indices/commodities/forex
        (quote data only), and treasuries (yield from /treasury-rates).

        Args:
            overrides: Optional Bloomberg-style overrides (e.g. EQY_FUND_RELATIVE_PERIOD)
                       to control which financial period is returned.
            instrument_type: "Equity", "Index", "Comdty", "Curncy", or "Treasury".
        """
        import asyncio

        # --- Constant fields (apply to all instrument types) ---
        constants: dict[str, Any] = {}
        if "PX_SCALING_FACTOR" in requested_fields:
            constants["PX_SCALING_FACTOR"] = 1.0

        # --- Treasury: special path ---
        if instrument_type == "Treasury":
            maturity_field = TREASURY_MATURITY_MAP.get(ticker)
            if not maturity_field:
                logger.warning(f"Unknown treasury maturity: {ticker}")
                return constants
            if hasattr(self.client, "fetch_treasury_rate"):
                treasury_data = await self.client.fetch_treasury_rate(maturity_field)  # type: ignore[attr-defined]
                result = self.map_fields(treasury_data, requested_fields)
                result.update(constants)
                return result
            logger.info(
                f"Treasury data requires FMP client (no fetch_treasury_rate on {type(self.client).__name__})"
            )
            return constants

        # --- Non-equity (Index / Comdty / Curncy): quote fields only ---
        if instrument_type in ("Index", "Comdty", "Curncy"):
            quote_data = await self.client.fetch_quote(ticker)
            result = self.map_fields(quote_data, requested_fields)
            result.update(constants)
            return result

        # --- Bond (Corp / Govt / Mtge / Muni): look up via ISIN → company notes ---
        if instrument_type == "Bond":
            result = await self._fetch_bond_data(ticker, requested_fields)
            result.update(constants)
            return result

        # --- Equity: full quote + fundamental routing ---
        period, offset = self._parse_period_overrides(overrides)

        quote_fields: list[str] = []
        fundamental_fields: list[str] = []
        needed_endpoints: set[str] = set()

        for field in requested_fields:
            if field in BLOOMBERG_TO_PROVIDER:
                quote_fields.append(field)
                continue

            field_info = FIELDS_LOOKUP.get((field, "ReferenceDataRequest"))
            if (
                field_info
                and field_info.openbb_mapping
                and field_info.openbb_mapping.startswith("fmp_")
            ):
                fundamental_fields.append(field)
                endpoint = field_info.openbb_mapping.split("__")[0].removeprefix("fmp_")
                needed_endpoints.add(endpoint)
            else:
                logger.debug(f"No mapping for reference field: {field}")

        result: dict[str, Any] = dict(constants)

        fetch_tasks: dict[str, Any] = {}
        if quote_fields:
            fetch_tasks["quote"] = self.client.fetch_quote(ticker)

        is_ttm = period == "ttm"

        TTM_METHOD_MAP = {
            "balance_sheet": "fetch_balance_sheet_ttm",
            "income_statement": "fetch_income_statement_ttm",
            "cash_flow": "fetch_cash_flow_ttm",
            "ratios": "fetch_ratios_ttm",
            "key_metrics": "fetch_key_metrics_ttm",
        }
        PERIOD_AWARE_ENDPOINTS = {
            "balance_sheet",
            "income_statement",
            "cash_flow",
            "ratios",
            "key_metrics",
        }
        STANDARD_METHOD_MAP = {
            "balance_sheet": "fetch_balance_sheet",
            "income_statement": "fetch_income_statement",
            "cash_flow": "fetch_cash_flow",
            "profile": "fetch_profile",
            "ratios": "fetch_ratios",
            "key_metrics": "fetch_key_metrics",
            "price_target_consensus": "fetch_price_target_consensus",
            "dividends_calendar": "fetch_dividends_calendar",
        }

        for endpoint in needed_endpoints:
            if is_ttm and endpoint in TTM_METHOD_MAP:
                method_name = TTM_METHOD_MAP[endpoint]
                if hasattr(self.client, method_name):
                    fetch_tasks[endpoint] = getattr(self.client, method_name)(ticker)
            elif endpoint in STANDARD_METHOD_MAP:
                method_name = STANDARD_METHOD_MAP[endpoint]
                if hasattr(self.client, method_name):
                    if endpoint in PERIOD_AWARE_ENDPOINTS:
                        fetch_tasks[endpoint] = getattr(self.client, method_name)(
                            ticker, period=period, offset=offset
                        )
                    else:
                        fetch_tasks[endpoint] = getattr(self.client, method_name)(ticker)

        if not fetch_tasks:
            return result

        endpoint_names = list(fetch_tasks.keys())
        endpoint_results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)

        endpoint_data: dict[str, dict] = {}
        first_error: BaseException | None = None
        for name, data in zip(endpoint_names, endpoint_results, strict=True):
            if isinstance(data, BaseException):
                logger.error(f"Error fetching {name} for {ticker}: {data}")
                endpoint_data[name] = {}
                if first_error is None:
                    first_error = data
            else:
                raw = data if isinstance(data, dict) else {}
                if is_ttm and name in ("ratios", "key_metrics"):
                    raw = {k.removesuffix("TTM"): v for k, v in raw.items()}
                endpoint_data[name] = raw

        all_empty = all(not v for v in endpoint_data.values())
        if all_empty and first_error is not None:
            raise first_error

        if quote_fields and "quote" in endpoint_data:
            result.update(self.map_fields(endpoint_data["quote"], quote_fields))

        for field in fundamental_fields:
            field_info = FIELDS_LOOKUP.get((field, "ReferenceDataRequest"))
            if not field_info or not field_info.openbb_mapping:
                continue

            parts = field_info.openbb_mapping.removeprefix("fmp_").split("__")
            if len(parts) != 2:
                continue
            endpoint, fmp_field = parts

            data = endpoint_data.get(endpoint, {})
            value = data.get(fmp_field)
            if value is not None:
                result[field] = value

        return result

    async def execute_ref_data_request(self, request: ReferenceDataRequest) -> dict[str, Any]:
        return {}
