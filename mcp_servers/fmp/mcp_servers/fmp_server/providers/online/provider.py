"""Online provider implementation for FMP MCP Server.

This module implements the OnlineProvider class that makes live API calls
to Financial Modeling Prep when operating in online mode.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

# Support both module and direct execution
try:
    from ...config import Mode, get_config
    from ..base import BaseProvider
    from .errors import api_error, invalid_api_key, network_error, rate_limited
except ImportError:
    # Direct execution - add parent to path for local imports
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from config import Mode, get_config
    from providers.base import BaseProvider
    from providers.online.errors import api_error, invalid_api_key, network_error, rate_limited


class OnlineProvider(BaseProvider):
    """Online provider making live API calls to FMP.

    This provider implements all 173 BaseProvider methods by calling
    the FMP API directly. API key validation happens at runtime on
    the first request, not at startup.

    Attributes:
        _api_key: The FMP API key from configuration
        _base_url: The FMP API base URL
        _api_key_validated: Whether the API key has been validated
    """

    def __init__(self) -> None:
        """Initialize the online provider."""
        super().__init__(mode=Mode.ONLINE, provider_name="fmp-api")
        config = get_config()
        self._api_key = config.fmp_api_key
        self._base_url = config.fmp_api_base_url.rstrip("/")
        self._api_key_validated = False
        self._config = config

    async def _make_request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        """Make an API request to FMP.

        Handles:
        - API key injection
        - Runtime API key validation (first request)
        - Error handling (rate limits, network errors, API errors)
        - Response metadata injection
        - v3/v4 API versioning (via _use_v3 or _use_v4 params)

        Args:
            endpoint: The API endpoint path (e.g., "/quote")
            params: Optional query parameters. Special keys:
                - _use_v3: Use legacy v3 API base URL
                - _use_v4: Use v4 API base URL
            method: HTTP method (default: GET)

        Returns:
            Response data with metadata, or error response.
        """
        if params is None:
            params = {}

        # Support v3/v4 API for endpoints not available in stable
        if params.pop("_use_v4", False):
            base_url = "https://financialmodelingprep.com/api/v4"
        elif params.pop("_use_v3", False):
            base_url = "https://financialmodelingprep.com/api/v3"
        else:
            base_url = self._base_url

        # Add API key to all requests
        if self._api_key:
            params["apikey"] = self._api_key
        else:
            return self._add_metadata(invalid_api_key())

        url = f"{base_url}{endpoint}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    response = await client.get(url, params=params)
                else:
                    response = await client.post(url, params=params)

                # Check for rate limiting
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    return self._add_metadata(
                        rate_limited(int(retry_after) if retry_after else None)
                    )

                # Check for unauthorized (invalid API key)
                if response.status_code == 401:
                    return self._add_metadata(invalid_api_key())

                # Check for other HTTP errors
                if response.status_code >= 400:
                    error_text = response.text[:200] if response.text else "Unknown error"
                    if response.status_code == 403:
                        lower_text = error_text.lower()
                        if "subscription" in lower_text or "exclusive endpoint" in lower_text:
                            error_text = (
                                f"{error_text}. "
                                "This endpoint requires a higher FMP subscription tier."
                            )
                    return self._add_metadata(api_error(response.status_code, error_text))

                # Parse JSON response
                try:
                    data = response.json()
                except Exception:
                    body_preview = response.text[:200] if response.text else "empty response"
                    content_type = response.headers.get("content-type", "unknown")
                    return self._add_metadata(
                        api_error(
                            response.status_code,
                            f"Invalid JSON response (content-type: {content_type}): {body_preview}",
                        )
                    )

                # Mark API key as validated on first successful request
                if not self._api_key_validated:
                    self._api_key_validated = True
                    logger.info("API key validated successfully")

                # Detect empty API responses (empty list, empty dict, or FMP error strings)
                if data is None or data == [] or data == {}:
                    return self._add_metadata({"data": data, "error": None, "_empty": True})
                if isinstance(data, dict) and "Error Message" in data:
                    return self._add_metadata(
                        {
                            "data": None,
                            "error": data["Error Message"],
                        }
                    )

                return self._add_metadata({"data": data, "error": None})

        except httpx.TimeoutException as e:
            logger.error(f"Timeout error for {endpoint}: {e}")
            return self._add_metadata(network_error("Request timed out"))
        except httpx.ConnectError as e:
            logger.error(f"Connection error for {endpoint}: {e}")
            return self._add_metadata(network_error("Failed to connect to API"))
        except httpx.RequestError as e:
            logger.error(f"Request error for {endpoint}: {e}")
            return self._add_metadata(network_error(str(e)))
        except Exception as e:
            logger.error(f"Unexpected error for {endpoint}: {e}")
            return self._add_metadata(network_error(str(e)))

    def _apply_limit(self, limit: int | None) -> int:
        """Apply default limit if None, cap at max."""
        return self._config.apply_limit(limit)

    # =========================================================================
    # Stock Quotes (quote.py) - 18 methods
    # =========================================================================

    async def get_stock_quote(self, symbol: str) -> dict[str, Any]:
        """Get real-time stock quote for a symbol."""
        result = await self._make_request("/quote", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        data = result.get("data", [])
        if data and isinstance(data, list) and len(data) > 0:
            return self._add_metadata(
                {
                    "symbol": symbol.upper(),
                    "quote": data[0],
                    "error": None,
                }
            )
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "quote": None,
                "error": "No data returned from API",
            }
        )

    async def get_stock_quote_short(self, symbol: str) -> dict[str, Any]:
        """Get condensed real-time stock quote."""
        result = await self._make_request("/quote-short", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_aftermarket_trade(self, symbol: str) -> dict[str, Any]:
        """Get aftermarket trade data for a symbol."""
        result = await self._make_request("/aftermarket-trade", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_aftermarket_quote(self, symbol: str) -> dict[str, Any]:
        """Get aftermarket quote data for a symbol."""
        result = await self._make_request("/aftermarket-quote", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_stock_price_change(self, symbol: str) -> dict[str, Any]:
        """Get stock price change over multiple periods."""
        result = await self._make_request("/stock-price-change", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_batch_stock_quotes(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for multiple stocks."""
        result = await self._make_request("/batch-quote", {"symbols": symbols})
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[:limit]
        return self._add_metadata(
            {
                "symbols": symbols,
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_batch_stock_quotes_short(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get condensed quotes for multiple stocks."""
        result = await self._make_request("/batch-quote-short", {"symbols": symbols})
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "symbols": symbols,
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_batch_aftermarket_trades(self, symbols: str) -> dict[str, Any]:
        """Get aftermarket trades for multiple stocks."""
        result = await self._make_request("/batch-aftermarket-trade", {"symbols": symbols})
        if result.get("error"):
            return result
        data = result.get("data", [])
        return self._add_metadata(
            {
                "symbols": symbols,
                "trades": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_batch_aftermarket_quotes(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get aftermarket quotes for multiple stocks."""
        result = await self._make_request("/batch-aftermarket-quote", {"symbols": symbols})
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "symbols": symbols,
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_exchange_stock_quotes(
        self, exchange: str, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all stocks on an exchange."""
        params: dict[str, Any] = {"exchange": exchange}
        if short is not None:
            params["short"] = str(short).lower()
        result = await self._make_request("/batch-exchange-quote", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "exchange": exchange,
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_all_mutualfund_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all mutual funds."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = str(short).lower()
        result = await self._make_request("/batch-mutualfund-quotes", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "asset_type": "mutual_funds",
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_all_etf_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all ETFs."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = str(short).lower()
        result = await self._make_request("/batch-etf-quotes", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "asset_type": "etfs",
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_all_commodity_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all commodities."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = str(short).lower()
        result = await self._make_request("/batch-commodity-quotes", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "asset_type": "commodities",
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_all_crypto_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all cryptocurrencies."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = str(short).lower()
        result = await self._make_request("/batch-crypto-quotes", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "asset_type": "cryptocurrencies",
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_all_forex_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all forex pairs."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = str(short).lower()
        result = await self._make_request("/batch-forex-quotes", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "asset_type": "forex",
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    async def get_all_index_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all market indexes."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = str(short).lower()
        result = await self._make_request("/batch-index-quotes", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit and isinstance(data, list):
            data = data[: self._apply_limit(limit)]
        return self._add_metadata(
            {
                "asset_type": "indexes",
                "quotes": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": None,
            }
        )

    # =========================================================================
    # Company Search (company_search.py) - 6 methods
    # =========================================================================

    async def search_by_symbol(
        self, query: str, limit: int | None = None, exchange: str | None = None
    ) -> dict[str, Any]:
        """Search companies by ticker symbol."""
        params: dict[str, Any] = {"query": query}
        if limit:
            params["limit"] = limit
        if exchange:
            params["exchange"] = exchange
        result = await self._make_request("/search-symbol", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "query": query,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def search_by_company_name(
        self, query: str, limit: int | None = None, exchange: str | None = None
    ) -> dict[str, Any]:
        """Search companies by name."""
        params: dict[str, Any] = {"query": query}
        if limit:
            params["limit"] = limit
        if exchange:
            params["exchange"] = exchange
        result = await self._make_request("/search-name", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data:
            error = f"No results found for company name '{query}'."
        return self._add_metadata(
            {
                "query": query,
                "data": data,
                "count": len(data),
                "error": error,
            }
        )

    async def search_by_cik(self, cik: str) -> dict[str, Any]:
        """Search companies by CIK number."""
        result = await self._make_request("/search-cik", {"cik": cik})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "cik": cik,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def search_by_cusip(self, cusip: str) -> dict[str, Any]:
        """Search companies by CUSIP identifier."""
        result = await self._make_request("/search-cusip", {"cusip": cusip})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "cusip": cusip,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def search_by_isin(self, isin: str) -> dict[str, Any]:
        """Search companies by ISIN identifier."""
        result = await self._make_request("/search-isin", {"isin": isin})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "isin": isin,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def screen_stocks(
        self,
        market_cap_more_than: int | None = None,
        market_cap_lower_than: int | None = None,
        price_more_than: float | None = None,
        price_lower_than: float | None = None,
        beta_more_than: float | None = None,
        beta_lower_than: float | None = None,
        volume_more_than: int | None = None,
        volume_lower_than: int | None = None,
        dividend_more_than: float | None = None,
        dividend_lower_than: float | None = None,
        is_etf: bool | None = None,
        is_fund: bool | None = None,
        is_actively_trading: bool | None = None,
        sector: str | None = None,
        industry: str | None = None,
        country: str | None = None,
        exchange: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Screen stocks based on various criteria."""
        params: dict[str, Any] = {}
        if market_cap_more_than is not None:
            params["marketCapMoreThan"] = market_cap_more_than
        if market_cap_lower_than is not None:
            params["marketCapLowerThan"] = market_cap_lower_than
        if price_more_than is not None:
            params["priceMoreThan"] = price_more_than
        if price_lower_than is not None:
            params["priceLowerThan"] = price_lower_than
        if beta_more_than is not None:
            params["betaMoreThan"] = beta_more_than
        if beta_lower_than is not None:
            params["betaLowerThan"] = beta_lower_than
        if volume_more_than is not None:
            params["volumeMoreThan"] = volume_more_than
        if volume_lower_than is not None:
            params["volumeLowerThan"] = volume_lower_than
        if dividend_more_than is not None:
            params["dividendMoreThan"] = dividend_more_than
        if dividend_lower_than is not None:
            params["dividendLowerThan"] = dividend_lower_than
        if is_etf is not None:
            params["isEtf"] = str(is_etf).lower()
        if is_fund is not None:
            params["isFund"] = str(is_fund).lower()
        if is_actively_trading is not None:
            params["isActivelyTrading"] = str(is_actively_trading).lower()
        if sector:
            params["sector"] = sector
        if industry:
            params["industry"] = industry
        if country:
            params["country"] = country
        if exchange:
            params["exchange"] = exchange
        if limit:
            params["limit"] = limit

        result = await self._make_request("/company-screener", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    # =========================================================================
    # Company Information (company_information.py) - 24 methods
    # =========================================================================

    async def get_company_profile(self, symbol: str) -> dict[str, Any]:
        """Get comprehensive company profile."""
        result = await self._make_request("/profile", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        data = result.get("data", [])
        if data and isinstance(data, list) and len(data) > 0:
            return self._add_metadata(
                {
                    "symbol": symbol.upper(),
                    "profile": data[0],
                    "error": None,
                }
            )
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "profile": None,
                "error": f"No company profile found for '{symbol.upper()}'.",
            }
        )

    async def get_company_notes(self, symbol: str) -> dict[str, Any]:
        """Get company notes and filings."""
        result = await self._make_request("/company-notes", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_company_executives(
        self, symbol: str, active: bool | None = None
    ) -> dict[str, Any]:
        """Get company executive information."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if active is not None:
            params["active"] = active
        result = await self._make_request("/key-executives", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_executive_compensation(self, symbol: str) -> dict[str, Any]:
        """Get executive compensation data."""
        result = await self._make_request(
            "/governance-executive-compensation", {"symbol": symbol.upper()}
        )
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_executive_comp_benchmark(self, year: int | None = None) -> dict[str, Any]:
        """Get executive compensation benchmarks."""
        params: dict[str, Any] = {}
        if year:
            params["year"] = year
        result = await self._make_request("/executive-compensation-benchmark", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_company_dividends(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company dividend history."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/dividends-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_company_earnings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company earnings data."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/earnings-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_stock_splits(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get stock split history."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/splits-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_stock_peers(self, symbol: str) -> dict[str, Any]:
        """Get peer companies for a stock."""
        result = await self._make_request("/stock-peers", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_market_cap(self, symbol: str) -> dict[str, Any]:
        """Get current market capitalization."""
        result = await self._make_request("/market-capitalization", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_market_cap(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical market capitalization."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/historical-market-capitalization", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_batch_market_cap(self, symbols: str) -> dict[str, Any]:
        """Get market cap for multiple symbols."""
        result = await self._make_request(
            "/market-capitalization-batch", {"symbols": symbols.upper()}
        )
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbols": symbols,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_shares_float(self, symbol: str) -> dict[str, Any]:
        """Get shares float data."""
        result = await self._make_request("/shares-float", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_all_shares_float(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get shares float for all companies."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/shares-float-all", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_employee_count(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company employee count."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/employee-count", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_employee_count(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get historical employee count."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/historical-employee-count", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_stock_grades(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get analyst stock grades."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/grades", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_revenue_by_geography(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get revenue breakdown by geography."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if period:
            params["period"] = period
        if limit:
            params["limit"] = self._apply_limit(limit)
        result = await self._make_request("/revenue-geographic-segmentation", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_revenue_by_product(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get revenue breakdown by product."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if period:
            params["period"] = period
        if limit:
            params["limit"] = self._apply_limit(limit)
        result = await self._make_request("/revenue-product-segmentation", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_profile_by_cik(self, cik: str) -> dict[str, Any]:
        """Get company profile by CIK."""
        result = await self._make_request("/profile-cik", {"cik": cik})
        if result.get("error"):
            return result
        data = result.get("data", [])
        if data and isinstance(data, list) and len(data) > 0:
            return self._add_metadata(
                {
                    "cik": cik,
                    "profile": data[0],
                    "error": None,
                }
            )
        return self._add_metadata(
            {
                "cik": cik,
                "profile": None,
                "error": "No data returned from API",
            }
        )

    async def get_enterprise_values(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get enterprise value data."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if period:
            params["period"] = period
        if limit:
            params["limit"] = limit
        result = await self._make_request("/enterprise-values", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_ratings_snapshot(self, symbol: str) -> dict[str, Any]:
        """Get current analyst ratings snapshot."""
        result = await self._make_request("/ratings-snapshot", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_ratings_historical(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get historical analyst ratings."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/ratings-historical", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_owner_earnings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get owner earnings data."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/owner-earnings", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Financial Statements (financial_statements.py) - 27 methods
    # =========================================================================

    async def get_income_statement(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/income-statement", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data or (isinstance(data, list) and len(data) == 0):
            error = f"No income statement data for {symbol.upper()} (period={period})."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": error,
            }
        )

    async def get_balance_sheet(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/balance-sheet-statement", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data or (isinstance(data, list) and len(data) == 0):
            error = f"No balance sheet data for {symbol.upper()} (period={period})."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": error,
            }
        )

    async def get_cash_flow_statement(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow statement data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/cash-flow-statement", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data or (isinstance(data, list) and len(data) == 0):
            error = f"No cash flow data for {symbol.upper()} (period={period})."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": error,
            }
        )

    async def get_latest_financials(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest financial statements across companies."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/latest-financial-statements", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "page": page,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_stocks_with_financials(self, page: int = 0) -> dict[str, Any]:
        """Get list of stocks with financial data."""
        params = {"page": page}
        result = await self._make_request("/financial-statement-symbol-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_income_statement_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months income statement."""
        result = await self._make_request("/income-statement-ttm", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_balance_sheet_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months balance sheet."""
        result = await self._make_request(
            "/balance-sheet-statement-ttm", {"symbol": symbol.upper()}
        )
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_cash_flow_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months cash flow."""
        result = await self._make_request("/cash-flow-statement-ttm", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_income_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/income-statement-as-reported", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_balance_sheet_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/balance-sheet-statement-as-reported", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data:
            error = f"No balance sheet (as reported) data for '{symbol.upper()}'."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": data,
                "error": error,
            }
        )

    async def get_cash_flow_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow statement as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/cash-flow-statement-as-reported", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_full_financials_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get full financial statements as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/financial-statement-full-as-reported", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_financial_report_json(
        self, symbol: str, year: int, period: str = "FY"
    ) -> dict[str, Any]:
        """Get financial report in JSON format."""
        result = await self._make_request(
            "/financial-reports-json",
            {"symbol": symbol.upper(), "year": year, "period": period},
        )
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "year": year,
                "period": period,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_financial_report_xlsx(
        self, symbol: str, year: int, period: str = "FY"
    ) -> dict[str, Any]:
        """Get financial report in XLSX format.

        Note: This endpoint returns binary Excel data, not JSON.
        We return a download URL instead of the binary content.
        """
        # Build the download URL - this endpoint returns binary Excel data
        download_url = (
            f"{self._base_url}/financial-reports-xlsx"
            f"?symbol={symbol.upper()}&year={year}&period={period}&apikey={self._api_key}"
        )
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "year": year,
                "period": period,
                "download_url": download_url,
                "note": "Returns Excel file. Use download_url to fetch binary data.",
                "error": None,
            }
        )

    async def get_financial_reports_dates(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get available financial report dates."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/financial-reports-dates", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_key_metrics(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get key financial metrics."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/key-metrics", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data or (isinstance(data, list) and len(data) == 0):
            error = f"No key metrics data for {symbol.upper()} (period={period})."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": data if isinstance(data, list) else [],
                "count": len(data) if isinstance(data, list) else 0,
                "error": error,
            }
        )

    async def get_key_metrics_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months key metrics."""
        result = await self._make_request("/key-metrics-ttm", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data or (isinstance(data, list) and len(data) == 0):
            error = f"No TTM key metrics data for {symbol.upper()}."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": data if isinstance(data, list) else [],
                "error": error,
            }
        )

    async def get_financial_ratios(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get financial ratios."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/ratios", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_ratios_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months ratios."""
        result = await self._make_request("/ratios-ttm", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_financial_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get financial growth metrics."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/financial-growth", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_income_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement growth metrics."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/income-statement-growth", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_balance_sheet_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet growth metrics."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/balance-sheet-statement-growth", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_cash_flow_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow growth metrics."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/cash-flow-statement-growth", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_financial_scores(self, symbol: str) -> dict[str, Any]:
        """Get financial health scores."""
        result = await self._make_request("/financial-scores", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Technical Indicators (technical_indicators.py) - 10 methods
    # =========================================================================

    async def _get_technical_indicator(
        self,
        indicator: str,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Helper to get technical indicators.

        Uses stable API: /technical-indicators/{indicator}?symbol=...&periodLength=...&timeframe=...
        """
        # Map indicator names to stable API paths
        indicator_path_map = {
            "sma": "sma",
            "ema": "ema",
            "wma": "wma",
            "dema": "dema",
            "tema": "tema",
            "rsi": "rsi",
            "williams": "williams",
            "adx": "adx",
            "standardDeviation": "standarddeviation",
        }
        api_indicator = indicator_path_map.get(indicator, indicator)

        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "periodLength": period,
            "timeframe": interval,
        }
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request(
            f"/technical-indicators/{api_indicator}",
            params,
        )
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "indicator": indicator,
                "period": period,
                "interval": interval,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_sma(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Simple Moving Average."""
        return await self._get_technical_indicator(
            "sma", symbol, period, interval, from_date, to_date
        )

    async def get_ema(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Exponential Moving Average."""
        return await self._get_technical_indicator(
            "ema", symbol, period, interval, from_date, to_date
        )

    async def get_wma(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Weighted Moving Average."""
        return await self._get_technical_indicator(
            "wma", symbol, period, interval, from_date, to_date
        )

    async def get_dema(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Double Exponential Moving Average."""
        return await self._get_technical_indicator(
            "dema", symbol, period, interval, from_date, to_date
        )

    async def get_tema(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Triple Exponential Moving Average."""
        return await self._get_technical_indicator(
            "tema", symbol, period, interval, from_date, to_date
        )

    async def get_williams(
        self,
        symbol: str,
        period: int = 14,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Williams %R indicator."""
        return await self._get_technical_indicator(
            "williams", symbol, period, interval, from_date, to_date
        )

    async def get_rsi(
        self,
        symbol: str,
        period: int = 14,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Relative Strength Index."""
        return await self._get_technical_indicator(
            "rsi", symbol, period, interval, from_date, to_date
        )

    async def get_adx(
        self,
        symbol: str,
        period: int = 14,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Average Directional Index."""
        return await self._get_technical_indicator(
            "adx", symbol, period, interval, from_date, to_date
        )

    async def get_standard_deviation(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Standard Deviation indicator."""
        return await self._get_technical_indicator(
            "standardDeviation", symbol, period, interval, from_date, to_date
        )

    # =========================================================================
    # Charts / Historical Prices (charts.py) - 11 methods
    # =========================================================================

    async def _get_historical_prices(
        self,
        symbol: str,
        endpoint: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Helper to get historical prices."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request(endpoint, params)
        if result.get("error"):
            return result
        data = result.get("data", {})
        historical = data.get("historical", []) if isinstance(data, dict) else data
        historical_list = historical if isinstance(historical, list) else []
        error = None
        if not historical_list:
            date_range_msg = ""
            if from_date and to_date:
                date_range_msg = f" for the period {from_date} to {to_date}"
            elif from_date:
                date_range_msg = f" from {from_date}"
            elif to_date:
                date_range_msg = f" up to {to_date}"
            error = f"No historical price data for {symbol.upper()}{date_range_msg}."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "historical": historical_list,
                "count": len(historical_list),
                "error": error,
            }
        )

    async def get_historical_price_full(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get full historical price data including all adjusted fields.

        Fetches both /full and /dividend-adjusted endpoints and merges
        adjOpen, adjHigh, adjLow, adjClose into the full data for complete
        price information.
        """
        # Fetch both endpoints in parallel for better performance
        full_result, adj_result = await asyncio.gather(
            self._get_historical_prices(symbol, "/historical-price-eod/full", from_date, to_date),
            self._get_historical_prices(
                symbol, "/historical-price-eod/dividend-adjusted", from_date, to_date
            ),
        )

        # If full request failed, return the error
        if full_result.get("error"):
            return full_result

        # Build a lookup of adjusted values by date
        adj_by_date: dict[str, dict[str, float | None]] = {}
        if not adj_result.get("error"):
            for record in adj_result.get("historical", []):
                date_key = record.get("date")
                if date_key:
                    adj_by_date[date_key] = {
                        "adjOpen": record.get("adjOpen"),
                        "adjHigh": record.get("adjHigh"),
                        "adjLow": record.get("adjLow"),
                        "adjClose": record.get("adjClose"),
                    }

        # Merge adjusted fields into full data
        for record in full_result.get("historical", []):
            date_key = record.get("date")
            if date_key and date_key in adj_by_date:
                adj_data = adj_by_date[date_key]
                if adj_data.get("adjOpen") is not None:
                    record["adjOpen"] = adj_data["adjOpen"]
                if adj_data.get("adjHigh") is not None:
                    record["adjHigh"] = adj_data["adjHigh"]
                if adj_data.get("adjLow") is not None:
                    record["adjLow"] = adj_data["adjLow"]
                if adj_data.get("adjClose") is not None:
                    record["adjClose"] = adj_data["adjClose"]

        return full_result

    async def get_historical_price_light(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get lightweight split-adjusted historical price data."""
        return await self._get_historical_prices(
            symbol, "/historical-price-eod/light", from_date, to_date
        )

    async def get_historical_price_dividend_adjusted(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get fully-adjusted historical prices (split-adjusted and dividend-adjusted)."""
        return await self._get_historical_prices(
            symbol, "/historical-price-eod/dividend-adjusted", from_date, to_date
        )

    async def get_historical_price_unadjusted(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get historical prices without dividend adjustments.

        Returns split-adjusted historical prices with open, high, low, close fields.
        Note: Despite the name, FMP's /historical-price-eod/full endpoint returns
        split-adjusted prices. For truly unadjusted intraday data, use the
        intraday endpoints with nonadjusted=true.
        """
        return await self._get_historical_prices(
            symbol, "/historical-price-eod/full", from_date, to_date
        )

    async def _get_intraday_prices(
        self,
        symbol: str,
        interval: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Helper to get intraday prices.

        Uses stable API: /historical-chart/{interval}?symbol=...
        """
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if nonadjusted is not None:
            params["nonadjusted"] = "true" if nonadjusted else "false"
        result = await self._make_request(f"/historical-chart/{interval}", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "interval": interval,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_intraday_1min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 1-minute intraday data."""
        return await self._get_intraday_prices(symbol, "1min", from_date, to_date, nonadjusted)

    async def get_intraday_5min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 5-minute intraday data."""
        return await self._get_intraday_prices(symbol, "5min", from_date, to_date, nonadjusted)

    async def get_intraday_15min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 15-minute intraday data."""
        return await self._get_intraday_prices(symbol, "15min", from_date, to_date, nonadjusted)

    async def get_intraday_30min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 30-minute intraday data."""
        return await self._get_intraday_prices(symbol, "30min", from_date, to_date, nonadjusted)

    async def get_intraday_1hour(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 1-hour intraday data."""
        return await self._get_intraday_prices(symbol, "1hour", from_date, to_date, nonadjusted)

    async def get_intraday_4hour(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 4-hour intraday data."""
        return await self._get_intraday_prices(symbol, "4hour", from_date, to_date, nonadjusted)

    # =========================================================================
    # Analyst (analyst.py) - 12 methods
    # =========================================================================

    async def get_analyst_estimates(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get analyst estimates."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/analyst-estimates", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data or (isinstance(data, list) and len(data) == 0):
            error = f"No analyst estimates for {symbol.upper()}."
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": data if isinstance(data, list) else [],
                "error": error,
            }
        )

    async def get_grades_consensus(self, symbol: str) -> dict[str, Any]:
        """Get analyst grades consensus."""
        result = await self._make_request("/grades-consensus", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_grades_historical(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get historical analyst grades."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/grades-historical", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_price_target_consensus(self, symbol: str) -> dict[str, Any]:
        """Get price target consensus."""
        result = await self._make_request("/price-target-consensus", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_price_target_summary(self, symbol: str) -> dict[str, Any]:
        """Get price target summary."""
        result = await self._make_request("/price-target-summary", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_price_target_news(
        self, symbol: str, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get price target news for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/price-target-news", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "page": page,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_price_target_latest_news(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest price target news across all stocks."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/price-target-latest-news", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "page": page,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_grade_news(
        self, symbol: str, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get upgrade/downgrade news with article links for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/grades-news", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_grade_latest_news(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest upgrade/downgrade news across all stocks."""
        params: dict[str, Any] = {"page": page, "_use_v4": True}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/upgrades-downgrades-rss-feed", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "page": page,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_latest_mergers_acquisitions(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest mergers & acquisitions."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/mergers-acquisitions-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def search_ma(self, query: str) -> dict[str, Any]:
        """Search for M&A deals by company name."""
        result = await self._make_request("/mergers-acquisitions-search", {"name": query})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "query": query,
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # News (news.py) - 10 methods
    # =========================================================================

    async def get_fmp_articles(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get FMP articles."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["size"] = limit
        result = await self._make_request("/fmp-articles", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_general_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest general news."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/general-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_stock_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest stock news."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/stock-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def search_stock_news_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search stock news by symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/stock", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_forex_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest forex news."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/forex-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def search_forex_news_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search forex news by symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/forex", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_crypto_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest crypto news."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/crypto-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def search_crypto_news_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search crypto news by symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/crypto", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_press_releases_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest press releases."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/press-releases-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def search_press_releases_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search press releases by symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/news/press-releases", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    # =========================================================================
    # Market Performance (market_performance.py) - 12 methods
    # =========================================================================

    async def get_sector_performance_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        sector: str | None = None,
    ) -> dict[str, Any]:
        """Get sector performance snapshot."""
        params: dict[str, Any] = {"date": date}
        if exchange:
            params["exchange"] = exchange
        if sector:
            params["sector"] = sector
        result = await self._make_request("/sector-performance-snapshot", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_industry_performance_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        industry: str | None = None,
    ) -> dict[str, Any]:
        """Get industry performance snapshot using P/E ratio data."""
        params: dict[str, Any] = {"date": date}
        if exchange:
            params["exchange"] = exchange
        if industry:
            params["industry"] = industry
        result = await self._make_request("/industry-performance-snapshot", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_sector_performance(
        self,
        sector: str,
        from_date: str | None = None,
        to_date: str | None = None,
        exchange: str | None = None,
    ) -> dict[str, Any]:
        """Get historical sector performance."""
        params: dict[str, Any] = {"sector": sector}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if exchange:
            params["exchange"] = exchange
        result = await self._make_request("/historical-sector-performance", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_industry_performance(
        self,
        industry: str,
        from_date: str | None = None,
        to_date: str | None = None,
        exchange: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical industry performance."""
        params: dict[str, Any] = {"industry": industry}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if exchange:
            params["exchange"] = exchange
        if limit:
            params["limit"] = limit
        result = await self._make_request("/historical-industry-performance", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_sector_pe_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        sector: str | None = None,
    ) -> dict[str, Any]:
        """Get sector P/E ratio snapshot."""
        params: dict[str, Any] = {"date": date}
        if exchange:
            params["exchange"] = exchange
        if sector:
            params["sector"] = sector
        result = await self._make_request("/sector-pe-snapshot", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_industry_pe_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        industry: str | None = None,
    ) -> dict[str, Any]:
        """Get industry P/E ratio snapshot."""
        params: dict[str, Any] = {"date": date}
        if exchange:
            params["exchange"] = exchange
        if industry:
            params["industry"] = industry
        result = await self._make_request("/industry-pe-snapshot", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_sector_pe(
        self,
        sector: str,
        exchange: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical sector P/E ratios."""
        params: dict[str, Any] = {"sector": sector}
        if exchange:
            params["exchange"] = exchange
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/historical-sector-pe", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "sector": sector,
                "exchange": exchange,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_industry_pe(
        self,
        industry: str,
        exchange: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical industry P/E ratios."""
        params: dict[str, Any] = {"industry": industry}
        if exchange:
            params["exchange"] = exchange
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/historical-industry-pe", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "industry": industry,
                "exchange": exchange,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_biggest_gainers(self) -> dict[str, Any]:
        """Get biggest gainers."""
        result = await self._make_request("/biggest-gainers", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_biggest_losers(self) -> dict[str, Any]:
        """Get biggest losers."""
        result = await self._make_request("/biggest-losers", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_most_actives(self) -> dict[str, Any]:
        """Get most active stocks."""
        result = await self._make_request("/most-actives", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Earnings/Dividends/Splits (earnings_dividends_splits.py) - 6 methods
    # =========================================================================

    async def get_earnings_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get earnings calendar."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/earnings-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_dividends_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get dividends calendar."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/dividends-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_splits_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get stock splits calendar."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/splits-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_ipos_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get IPO calendar."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/ipos-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_ipos_prospectus(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get IPO prospectus data."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/ipos-prospectus", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_ipos_disclosure(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get IPO disclosure data."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/ipos-disclosure", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Earnings Transcript (earnings_transcript.py) - 5 methods
    # =========================================================================

    async def get_earning_call_transcript(
        self, symbol: str, year: int, quarter: int
    ) -> dict[str, Any]:
        """Get earnings call transcript."""
        result = await self._make_request(
            "/earning-call-transcript",
            {"symbol": symbol.upper(), "year": year, "quarter": quarter},
        )
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "year": year,
                "quarter": quarter,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_transcript_dates_by_symbol(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get available transcript dates for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/earning-call-transcript-dates", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_companies_with_transcripts(self, limit: int | None = None) -> dict[str, Any]:
        """Get companies with earnings transcripts."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/earnings-transcript-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_latest_earning_transcripts(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest earnings transcripts across companies."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/earning-call-transcript-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "page": page,
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # DCF Valuation (dcf_valuation.py) - 4 methods
    # =========================================================================

    async def get_dcf_valuation(self, symbol: str) -> dict[str, Any]:
        """Get DCF valuation."""
        result = await self._make_request("/discounted-cash-flow", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_levered_dcf_valuation(self, symbol: str) -> dict[str, Any]:
        """Get levered DCF valuation."""
        result = await self._make_request(
            "/levered-discounted-cash-flow", {"symbol": symbol.upper()}
        )
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_custom_dcf_valuation(
        self,
        symbol: str,
        revenue_growth: float | None = None,
        ebitda_margin: float | None = None,
        cost_of_equity: float | None = None,
        terminal_growth: float | None = None,
        tax_rate: float | None = None,
        cost_of_debt: float | None = None,
        beta: float | None = None,
        risk_free_rate: float | None = None,
    ) -> dict[str, Any]:
        """Get custom DCF valuation with parameters."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if revenue_growth is not None:
            params["revenueGrowth"] = revenue_growth
        if ebitda_margin is not None:
            params["ebitdaMargin"] = ebitda_margin
        if cost_of_equity is not None:
            params["costOfEquity"] = cost_of_equity
        if terminal_growth is not None:
            params["terminalGrowth"] = terminal_growth
        if tax_rate is not None:
            params["taxRate"] = tax_rate
        if cost_of_debt is not None:
            params["costOfDebt"] = cost_of_debt
        if beta is not None:
            params["beta"] = beta
        if risk_free_rate is not None:
            params["riskFreeRate"] = risk_free_rate
        result = await self._make_request("/custom-discounted-cash-flow", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_custom_levered_dcf_valuation(
        self,
        symbol: str,
        revenue_growth: float | None = None,
        ebitda_margin: float | None = None,
        cost_of_equity: float | None = None,
        terminal_growth: float | None = None,
        tax_rate: float | None = None,
        cost_of_debt: float | None = None,
        beta: float | None = None,
        risk_free_rate: float | None = None,
    ) -> dict[str, Any]:
        """Get custom levered DCF valuation with parameters."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if revenue_growth is not None:
            params["revenueGrowth"] = revenue_growth
        if ebitda_margin is not None:
            params["ebitdaMargin"] = ebitda_margin
        if cost_of_equity is not None:
            params["costOfEquity"] = cost_of_equity
        if terminal_growth is not None:
            params["terminalGrowth"] = terminal_growth
        if tax_rate is not None:
            params["taxRate"] = tax_rate
        if cost_of_debt is not None:
            params["costOfDebt"] = cost_of_debt
        if beta is not None:
            params["beta"] = beta
        if risk_free_rate is not None:
            params["riskFreeRate"] = risk_free_rate
        result = await self._make_request("/custom-levered-discounted-cash-flow", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Economics (economics.py) - 4 methods
    # =========================================================================

    async def get_treasury_rates(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get treasury rates."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/treasury-rates", params)
        if result.get("error"):
            return result
        data = result.get("data", [])
        error = None
        if not data or (isinstance(data, list) and len(data) == 0):
            date_range_msg = ""
            if from_date and to_date:
                date_range_msg = f" for the period {from_date} to {to_date}"
            elif from_date:
                date_range_msg = f" from {from_date}"
            elif to_date:
                date_range_msg = f" up to {to_date}"
            error = f"No treasury rates data{date_range_msg}."
        return self._add_metadata(
            {
                "data": data if isinstance(data, list) else [],
                "error": error,
            }
        )

    async def get_economic_indicators(
        self,
        indicator: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get economic indicators."""
        params: dict[str, Any] = {"name": indicator}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/economic-indicators", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "indicator": indicator,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_economic_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get economic calendar."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if limit:
            params["limit"] = limit
        result = await self._make_request("/economic-calendar", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_market_risk_premium(self) -> dict[str, Any]:
        """Get market risk premium."""
        result = await self._make_request("/market-risk-premium", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # ETF/Mutual Funds (etf_mutual_funds.py) - 9 methods
    # =========================================================================

    async def get_etf_holdings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get ETF holdings."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/etf/holdings", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_etf_info(self, symbol: str) -> dict[str, Any]:
        """Get ETF information."""
        result = await self._make_request("/etf/info", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_etf_sector_weightings(self, symbol: str) -> dict[str, Any]:
        """Get ETF sector weightings."""
        result = await self._make_request("/etf/sector-weightings", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_etf_country_weightings(self, symbol: str) -> dict[str, Any]:
        """Get ETF country weightings."""
        result = await self._make_request("/etf/country-weightings", {"symbol": symbol.upper()})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_etf_asset_exposure(self, symbol: str) -> dict[str, Any]:
        """Get ETF asset class exposure."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        result = await self._make_request("/etf/asset-exposure", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_fund_disclosure(
        self,
        symbol: str,
        year: str,
        quarter: str,
        cik: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get fund disclosure."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "year": year, "quarter": quarter}
        if cik:
            params["cik"] = cik
        if limit:
            params["limit"] = limit
        result = await self._make_request("/funds/disclosure", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_fund_disclosure_dates(
        self, symbol: str, cik: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get fund disclosure dates."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if cik:
            params["cik"] = cik
        if limit:
            params["limit"] = limit
        result = await self._make_request("/funds/disclosure-dates", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_fund_disclosure_holders_latest(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest fund disclosure holders."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/funds/disclosure-holders-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def search_fund_disclosure_by_name(
        self, name: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Search fund disclosures by name."""
        params: dict[str, Any] = {"name": name}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/funds/disclosure-holders-search", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "name": name,
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Stock Directory (stock_directory.py) - 14 methods
    # =========================================================================

    async def get_all_stock_symbols(self, limit: int | None = None) -> dict[str, Any]:
        """Get all stock symbols."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/stock-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_all_etfs(self, limit: int | None = None) -> dict[str, Any]:
        """Get all ETF symbols."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/etf-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_supported_exchanges(self) -> dict[str, Any]:
        """Get list of supported exchanges."""
        result = await self._make_request("/available-exchanges", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def find_exchange_listings(
        self, exchange: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Find listings for an exchange.

        Uses stable /company-screener with exchange filter.
        """
        params: dict[str, Any] = {"exchange": exchange}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/company-screener", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "exchange": exchange,
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_actively_trading_stocks(self, limit: int | None = None) -> dict[str, Any]:
        """Get actively trading stocks."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/actively-trading-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_delisted_companies(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get delisted companies."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/delisted-companies", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_cik_database(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get CIK database."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/cik-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_all_countries(self) -> dict[str, Any]:
        """Get all countries."""
        result = await self._make_request("/available-countries", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_all_sectors(self) -> dict[str, Any]:
        """Get all sectors."""
        result = await self._make_request("/available-sectors", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_all_industries(self) -> dict[str, Any]:
        """Get all industries."""
        result = await self._make_request("/available-industries", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_recent_symbol_changes(
        self, invalid: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get recent symbol changes."""
        params: dict[str, Any] = {}
        if invalid is not None:
            params["invalid"] = invalid
        if limit:
            params["limit"] = limit
        result = await self._make_request("/symbol-change", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_cryptocurrency_list(self, limit: int | None = None) -> dict[str, Any]:
        """Get cryptocurrency list."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/cryptocurrency-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_forex_currency_pairs(self, limit: int | None = None) -> dict[str, Any]:
        """Get forex currency pairs."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/forex-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_commodities_list(self, limit: int | None = None) -> dict[str, Any]:
        """Get commodities list."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        result = await self._make_request("/commodities-list", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    # =========================================================================
    # Indexes (indexes.py) - 7 methods
    # =========================================================================

    async def get_index_list(self) -> dict[str, Any]:
        """Get list of available indexes."""
        result = await self._make_request("/index-list", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_sp500_constituents(self, limit: int | None = None) -> dict[str, Any]:
        """Get S&P 500 constituents."""
        result = await self._make_request("/sp500-constituent", {})
        if result.get("error"):
            return result
        data = result.get("data", [])
        if limit is not None:
            data = data[:limit]
        return self._add_metadata(
            {
                "data": data,
                "count": len(data),
                "error": None,
            }
        )

    async def get_nasdaq_constituents(self) -> dict[str, Any]:
        """Get NASDAQ constituents."""
        result = await self._make_request("/nasdaq-constituent", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_dowjones_constituents(self) -> dict[str, Any]:
        """Get Dow Jones constituents."""
        result = await self._make_request("/dowjones-constituent", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "count": len(result.get("data", [])),
                "error": None,
            }
        )

    async def get_historical_sp500(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get historical S&P 500 constituents."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/historical-sp500-constituent", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_nasdaq(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get historical NASDAQ constituents."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/historical-nasdaq-constituent", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_historical_dowjones(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get historical Dow Jones constituents."""
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = await self._make_request("/historical-dowjones-constituent", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Market Hours (market_hours.py) - 3 methods
    # =========================================================================

    async def get_all_exchange_market_hours(self) -> dict[str, Any]:
        """Get market hours for all exchanges."""
        result = await self._make_request("/all-exchange-market-hours", {})
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_exchange_market_hours(self, exchange: str) -> dict[str, Any]:
        """Get market hours for a specific exchange."""
        params = {"exchange": exchange}
        result = await self._make_request("/exchange-market-hours", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "exchange": exchange,
                "data": result.get("data", []),
                "error": None,
            }
        )

    async def get_holidays_by_exchange(
        self,
        exchange: str,
        from_date: str | None = None,
        to_date: str | None = None,
        year: int | None = None,
    ) -> dict[str, Any]:
        """Get holidays for an exchange."""
        params: dict[str, Any] = {"exchange": exchange}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if year:
            params["year"] = year
        result = await self._make_request("/holidays-by-exchange", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "exchange": exchange,
                "data": result.get("data", []),
                "error": None,
            }
        )

    # =========================================================================
    # Congressional Trading - 4 methods
    # =========================================================================

    async def get_house_disclosure(
        self,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get latest House disclosure filings."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        result = await self._make_request("/house-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_senate_disclosure(
        self,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get latest Senate disclosure filings."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        result = await self._make_request("/senate-latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_senate_trades(
        self,
        symbol: str,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get Senate trading activity for a specific symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        result = await self._make_request("/senate-trades", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_house_trades(
        self,
        symbol: str,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get House trading activity for a specific symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        result = await self._make_request("/house-trades", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "symbol": symbol.upper(),
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    # =========================================================================
    # SEC Filings - 2 methods
    # =========================================================================

    async def get_institutional_ownership(
        self,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get latest institutional ownership filings."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        result = await self._make_request("/institutional-ownership/latest", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )

    async def get_8k_filings(
        self,
        page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get latest 8-K SEC filings."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        result = await self._make_request("/sec-filings-8k", params)
        if result.get("error"):
            return result
        return self._add_metadata(
            {
                "data": result.get("data", []),
                "page": page,
                "error": None,
            }
        )
