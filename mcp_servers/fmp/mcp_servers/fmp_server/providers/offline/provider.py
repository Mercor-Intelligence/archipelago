"""Offline provider implementation for FMP MCP Server.

This module implements the OfflineProvider class that serves data from
the local SQLite database when operating in offline mode.
"""

import json
import sys
import time
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# Support both module and direct execution
try:
    from ...config import Mode
    from ...db.models import (
        Company,
        FinancialStatement,
        GenericApiResponse,
        HistoricalPrice,
        StockQuote,
        TechnicalIndicator,
    )
    from ...db.session import get_session
    from ..base import BaseProvider
    from .errors import (
        data_not_available,
        database_error,
        not_supported,
        symbol_not_available,
    )
except ImportError:
    # Direct execution - add parent to path for local imports
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from config import Mode
    from db.models import (
        Company,
        FinancialStatement,
        GenericApiResponse,
        HistoricalPrice,
        StockQuote,
        TechnicalIndicator,
    )
    from db.session import get_session
    from providers.base import BaseProvider
    from providers.offline.errors import (
        data_not_available,
        database_error,
        not_supported,
        symbol_not_available,
    )


class OfflineProvider(BaseProvider):
    """Offline provider serving data from local SQLite database.

    This provider implements all 173 BaseProvider methods by querying
    the local database populated by the download_fixtures script.

    Data is served from these tables:
    - companies: Company profiles and metadata
    - stock_quotes: Latest quote snapshots
    - historical_prices: Daily OHLCV data
    - financial_statements: Income, balance sheet, cash flow
    - technical_indicators: SMA, EMA, RSI, etc.
    - generic_api_responses: Catch-all for other endpoints

    Attributes:
        SYMBOL_CACHE_TTL_SECONDS: Time-to-live for the symbol cache (default: 300s/5min)
    """

    SYMBOL_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes

    def __init__(self, symbol_cache_ttl: float | None = None) -> None:
        """Initialize the offline provider.

        Args:
            symbol_cache_ttl: Optional TTL in seconds for the symbol cache.
                              Defaults to SYMBOL_CACHE_TTL_SECONDS (300s).
        """
        super().__init__(mode=Mode.OFFLINE, provider_name="fmp-fixtures")
        self._available_symbols: set[str] | None = None
        self._symbols_cache_timestamp: float | None = None
        self._symbol_cache_ttl = (
            symbol_cache_ttl if symbol_cache_ttl is not None else self.SYMBOL_CACHE_TTL_SECONDS
        )

    def invalidate_symbol_cache(self) -> None:
        """Invalidate the cached available symbols.

        Call this method after updating the database to ensure
        the provider picks up new symbols on the next request.
        """
        self._available_symbols = None
        self._symbols_cache_timestamp = None
        logger.debug("Symbol cache invalidated")

    def _is_symbol_cache_valid(self) -> bool:
        """Check if the symbol cache is still valid based on TTL.

        Returns:
            True if cache exists and hasn't expired, False otherwise.
        """
        if self._available_symbols is None or self._symbols_cache_timestamp is None:
            return False
        elapsed = time.monotonic() - self._symbols_cache_timestamp
        return elapsed < self._symbol_cache_ttl

    async def _get_available_symbols(self, session: AsyncSession) -> set[str]:
        """Get set of symbols available in the database.

        Caches the result for performance with automatic TTL-based expiration.
        The cache automatically refreshes after SYMBOL_CACHE_TTL_SECONDS.
        """
        if not self._is_symbol_cache_valid():
            result = await session.execute(select(Company.symbol))
            self._available_symbols = {row[0] for row in result.fetchall()}
            self._symbols_cache_timestamp = time.monotonic()
            logger.debug(f"Symbol cache refreshed with {len(self._available_symbols)} symbols")
        return self._available_symbols

    async def _check_symbol_available(
        self, session: AsyncSession, symbol: str
    ) -> dict[str, Any] | None:
        """Check if symbol is available, return error response if not.

        Returns None if symbol is available, otherwise returns error dict.
        """
        available = await self._get_available_symbols(session)
        if symbol.upper() not in available:
            return symbol_not_available(symbol, list(available)[:10])
        return None

    def _parse_json_field(self, json_str: str | None) -> Any:
        """Parse JSON field from database, return empty dict on failure."""
        if not json_str:
            return {}
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return {}

    def _compute_params_hash(self, params: dict[str, Any]) -> str:
        """Compute SHA256 hash of sorted parameters for generic lookup."""
        sorted_params = json.dumps(params, sort_keys=True, default=str)
        return sha256(sorted_params.encode()).hexdigest()

    @staticmethod
    def _escape_like_wildcards(query: str) -> str:
        """Escape SQL LIKE wildcards in user query strings.

        Escapes %, _, and \\ characters so they are treated as literals
        in LIKE/ILIKE patterns rather than wildcards.
        """
        return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    async def _get_generic_response(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Get response from generic_api_responses table.

        Args:
            endpoint: The API endpoint path
            params: Optional query parameters
            symbol: Optional symbol for the request

        Returns:
            Response data with metadata, or error if not found.
        """
        params = params or {}
        params_hash = self._compute_params_hash(params)

        try:
            async with get_session() as session:
                result = await session.execute(
                    select(GenericApiResponse).where(
                        GenericApiResponse.endpoint == endpoint,
                        GenericApiResponse.params_hash == params_hash,
                    )
                )
                record = result.scalar_one_or_none()

                if record:
                    data = self._parse_json_field(record.response_data)
                    return self._add_metadata({"data": data, "error": None})

                # Try without params hash for more flexible matching
                # Use .first() instead of .scalar_one_or_none() because multiple
                # records may exist with same endpoint+symbol but different params_hash
                if symbol:
                    result = await session.execute(
                        select(GenericApiResponse).where(
                            GenericApiResponse.endpoint == endpoint,
                            GenericApiResponse.symbol == symbol.upper(),
                        )
                    )
                    record = result.scalars().first()
                    if record:
                        data = self._parse_json_field(record.response_data)
                        return self._add_metadata({"data": data, "error": None})

                return self._add_metadata(data_not_available(f"{endpoint} data", symbol))

        except Exception as e:
            logger.error(f"Database error in _get_generic_response: {e}")
            return self._add_metadata(database_error(str(e)))

    # =========================================================================
    # Stock Quotes (quote.py) - 18 methods
    # =========================================================================

    async def get_stock_quote(self, symbol: str) -> dict[str, Any]:
        """Get real-time stock quote for a symbol."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol == symbol.upper())
                )
                quote = result.scalar_one_or_none()

                if quote:
                    quote_data = self._parse_json_field(quote.quote_data)
                    # If no stored JSON, construct from fields
                    if not quote_data:
                        quote_data = {
                            "symbol": quote.symbol,
                            "price": quote.price,
                            "open": quote.open,
                            "high": quote.high,
                            "low": quote.low,
                            "previousClose": quote.previous_close,
                            "change": quote.change,
                            "changesPercentage": quote.change_percent,
                            "volume": quote.volume,
                            "avgVolume": quote.avg_volume,
                            "marketCap": quote.market_cap,
                            "pe": quote.pe_ratio,
                            "eps": quote.eps,
                            "yearHigh": quote.fifty_two_week_high,
                            "yearLow": quote.fifty_two_week_low,
                            "sharesOutstanding": quote.shares_outstanding,
                        }
                    return self._add_metadata(
                        {
                            "symbol": symbol.upper(),
                            "quote": quote_data,
                            "error": None,
                        }
                    )

                return self._add_metadata(data_not_available("quote", symbol))

        except Exception as e:
            logger.error(f"Database error in get_stock_quote: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_stock_quote_short(self, symbol: str) -> dict[str, Any]:
        """Get condensed real-time stock quote."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol == symbol.upper())
                )
                quote = result.scalar_one_or_none()

                if quote:
                    data = [
                        {
                            "symbol": quote.symbol,
                            "price": quote.price,
                            "volume": quote.volume,
                        }
                    ]
                    return self._add_metadata(
                        {
                            "symbol": symbol.upper(),
                            "data": data,
                            "error": None,
                        }
                    )

                return self._add_metadata(data_not_available("quote", symbol))

        except Exception as e:
            logger.error(f"Database error in get_stock_quote_short: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_aftermarket_trade(self, symbol: str) -> dict[str, Any]:
        """Get aftermarket trade data for a symbol."""
        return await self._get_generic_response(
            "/aftermarket-trade", {"symbol": symbol.upper()}, symbol
        )

    async def get_aftermarket_quote(self, symbol: str) -> dict[str, Any]:
        """Get aftermarket quote data for a symbol."""
        return await self._get_generic_response(
            "/aftermarket-quote", {"symbol": symbol.upper()}, symbol
        )

    async def get_stock_price_change(self, symbol: str) -> dict[str, Any]:
        """Get stock price change over multiple periods."""
        return await self._get_generic_response(
            "/stock-price-change", {"symbol": symbol.upper()}, symbol
        )

    async def get_batch_stock_quotes(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for multiple stocks."""
        try:
            symbol_list = [s.strip().upper() for s in symbols.split(",")]
            async with get_session() as session:
                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol.in_(symbol_list))
                )
                quotes = result.scalars().all()

                quote_list = []
                for quote in quotes:
                    quote_data = self._parse_json_field(quote.quote_data)
                    if not quote_data:
                        quote_data = {
                            "symbol": quote.symbol,
                            "price": quote.price,
                            "volume": quote.volume,
                            "change": quote.change,
                            "changesPercentage": quote.change_percent,
                        }
                    quote_list.append(quote_data)

                if limit:
                    quote_list = quote_list[:limit]

                return self._add_metadata(
                    {
                        "symbols": symbols,
                        "quotes": quote_list,
                        "count": len(quote_list),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_batch_stock_quotes: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_batch_stock_quotes_short(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get condensed quotes for multiple stocks."""
        try:
            symbol_list = [s.strip().upper() for s in symbols.split(",")]
            async with get_session() as session:
                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol.in_(symbol_list))
                )
                quotes = result.scalars().all()

                quote_list = [
                    {"symbol": q.symbol, "price": q.price, "volume": q.volume} for q in quotes
                ]

                if limit:
                    quote_list = quote_list[:limit]

                return self._add_metadata(
                    {
                        "symbols": symbols,
                        "quotes": quote_list,
                        "count": len(quote_list),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_batch_stock_quotes_short: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_batch_aftermarket_trades(self, symbols: str) -> dict[str, Any]:
        """Get aftermarket trades for multiple stocks."""
        return await self._get_generic_response("/batch-aftermarket-trade", {"symbols": symbols})

    async def get_batch_aftermarket_quotes(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get aftermarket quotes for multiple stocks."""
        params: dict[str, Any] = {"symbols": symbols}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/batch-aftermarket-quote", params)

    async def get_exchange_stock_quotes(
        self, exchange: str, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all stocks on an exchange."""
        try:
            async with get_session() as session:
                # Get companies on this exchange
                result = await session.execute(
                    select(Company.symbol).where(
                        func.upper(Company.exchange_short_name) == exchange.upper()
                    )
                )
                exchange_symbols = [row[0] for row in result.fetchall()]

                if not exchange_symbols:
                    return self._add_metadata(
                        {
                            "exchange": exchange,
                            "quotes": [],
                            "count": 0,
                            "error": None,
                        }
                    )

                # Get quotes for these symbols
                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol.in_(exchange_symbols))
                )
                quotes = result.scalars().all()

                if short:
                    quote_list = [
                        {"symbol": q.symbol, "price": q.price, "volume": q.volume} for q in quotes
                    ]
                else:
                    quote_list = []
                    for q in quotes:
                        quote_data = self._parse_json_field(q.quote_data)
                        if not quote_data:
                            quote_data = {
                                "symbol": q.symbol,
                                "price": q.price,
                                "volume": q.volume,
                            }
                        quote_list.append(quote_data)

                if limit:
                    quote_list = quote_list[:limit]

                return self._add_metadata(
                    {
                        "exchange": exchange,
                        "quotes": quote_list,
                        "count": len(quote_list),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_exchange_stock_quotes: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_all_mutualfund_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all mutual funds."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = short
        if limit is not None:
            params["limit"] = limit
        return await self._get_generic_response("/batch-mutualfund-quotes", params)

    async def get_all_etf_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all ETFs."""
        try:
            async with get_session() as session:
                # Get ETF companies
                result = await session.execute(
                    select(Company.symbol).where(Company.is_etf == True)  # noqa: E712
                )
                etf_symbols = [row[0] for row in result.fetchall()]

                if not etf_symbols:
                    return self._add_metadata(
                        {
                            "asset_type": "etfs",
                            "quotes": [],
                            "count": 0,
                            "error": None,
                        }
                    )

                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol.in_(etf_symbols))
                )
                quotes = result.scalars().all()

                if short:
                    quote_list = [
                        {"symbol": q.symbol, "price": q.price, "volume": q.volume} for q in quotes
                    ]
                else:
                    quote_list = []
                    for q in quotes:
                        quote_data = self._parse_json_field(q.quote_data)
                        if not quote_data:
                            quote_data = {"symbol": q.symbol, "price": q.price}
                        quote_list.append(quote_data)

                if limit:
                    quote_list = quote_list[:limit]

                return self._add_metadata(
                    {
                        "asset_type": "etfs",
                        "quotes": quote_list,
                        "count": len(quote_list),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_all_etf_quotes: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_all_commodity_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all commodities."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = short
        if limit is not None:
            params["limit"] = limit
        return await self._get_generic_response("/batch-commodity-quotes", params)

    async def get_all_crypto_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all cryptocurrencies."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = short
        if limit is not None:
            params["limit"] = limit
        return await self._get_generic_response("/batch-crypto-quotes", params)

    async def get_all_forex_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all forex pairs."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = short
        if limit is not None:
            params["limit"] = limit
        return await self._get_generic_response("/batch-forex-quotes", params)

    async def get_all_index_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all market indexes."""
        params: dict[str, Any] = {}
        if short is not None:
            params["short"] = short
        if limit is not None:
            params["limit"] = limit
        return await self._get_generic_response("/batch-index-quotes", params)

    # =========================================================================
    # Company Search (company_search.py) - 6 methods
    # =========================================================================

    async def search_by_symbol(
        self, query: str, limit: int | None = None, exchange: str | None = None
    ) -> dict[str, Any]:
        """Search companies by ticker symbol."""
        try:
            async with get_session() as session:
                escaped_query = self._escape_like_wildcards(query)
                stmt = select(Company).where(
                    Company.symbol.ilike(f"%{escaped_query}%", escape="\\")
                )
                if exchange:
                    stmt = stmt.where(func.upper(Company.exchange_short_name) == exchange.upper())

                result = await session.execute(stmt)
                companies = result.scalars().all()

                data = [
                    {
                        "symbol": c.symbol,
                        "name": c.company_name,
                        "exchange": c.exchange_short_name,
                        "exchangeShortName": c.exchange_short_name,
                        "currency": c.currency,
                    }
                    for c in companies
                ]

                if limit:
                    data = data[:limit]

                return self._add_metadata(
                    {
                        "query": query,
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in search_by_symbol: {e}")
            return self._add_metadata(database_error(str(e)))

    async def search_by_company_name(
        self, query: str, limit: int | None = None, exchange: str | None = None
    ) -> dict[str, Any]:
        """Search companies by name."""
        try:
            async with get_session() as session:
                escaped_query = self._escape_like_wildcards(query)
                stmt = select(Company).where(
                    Company.company_name.ilike(f"%{escaped_query}%", escape="\\")
                )
                if exchange:
                    stmt = stmt.where(func.upper(Company.exchange_short_name) == exchange.upper())

                result = await session.execute(stmt)
                companies = result.scalars().all()

                data = [
                    {
                        "symbol": c.symbol,
                        "name": c.company_name,
                        "exchange": c.exchange_short_name,
                        "exchangeShortName": c.exchange_short_name,
                        "currency": c.currency,
                    }
                    for c in companies
                ]

                if limit:
                    data = data[:limit]

                error = None
                if not data:
                    error = (
                        f"No results found for company name '{query}'. "
                        "Try alternative spellings, the full legal name, "
                        "or search by ticker symbol instead."
                    )

                return self._add_metadata(
                    {
                        "query": query,
                        "data": data,
                        "count": len(data),
                        "error": error,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in search_by_company_name: {e}")
            return self._add_metadata(database_error(str(e)))

    async def search_by_cik(self, cik: str) -> dict[str, Any]:
        """Search companies by CIK number."""
        try:
            async with get_session() as session:
                result = await session.execute(select(Company).where(Company.cik == cik))
                company = result.scalar_one_or_none()

                if company:
                    data = [
                        {
                            "symbol": company.symbol,
                            "name": company.company_name,
                            "cik": company.cik,
                            "exchange": company.exchange_short_name,
                        }
                    ]
                else:
                    data = []

                return self._add_metadata(
                    {
                        "cik": cik,
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in search_by_cik: {e}")
            return self._add_metadata(database_error(str(e)))

    async def search_by_cusip(self, cusip: str) -> dict[str, Any]:
        """Search companies by CUSIP identifier."""
        try:
            async with get_session() as session:
                result = await session.execute(select(Company).where(Company.cusip == cusip))
                company = result.scalar_one_or_none()

                if company:
                    data = [
                        {
                            "symbol": company.symbol,
                            "name": company.company_name,
                            "cusip": company.cusip,
                            "exchange": company.exchange_short_name,
                        }
                    ]
                else:
                    data = []

                return self._add_metadata(
                    {
                        "cusip": cusip,
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in search_by_cusip: {e}")
            return self._add_metadata(database_error(str(e)))

    async def search_by_isin(self, isin: str) -> dict[str, Any]:
        """Search companies by ISIN identifier."""
        try:
            async with get_session() as session:
                result = await session.execute(select(Company).where(Company.isin == isin))
                company = result.scalar_one_or_none()

                if company:
                    data = [
                        {
                            "symbol": company.symbol,
                            "name": company.company_name,
                            "isin": company.isin,
                            "exchange": company.exchange_short_name,
                        }
                    ]
                else:
                    data = []

                return self._add_metadata(
                    {
                        "isin": isin,
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in search_by_isin: {e}")
            return self._add_metadata(database_error(str(e)))

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
        try:
            # Track unsupported filters that were requested
            unsupported_filters: list[str] = []
            if beta_more_than is not None or beta_lower_than is not None:
                unsupported_filters.append("beta")
                logger.warning("beta filters not supported in offline mode (beta data not stored)")
            if dividend_more_than is not None or dividend_lower_than is not None:
                unsupported_filters.append("dividend")
                logger.warning(
                    "dividend filters not supported in offline mode (dividend data not stored)"
                )
            if is_fund is not None:
                unsupported_filters.append("is_fund")
                logger.warning(
                    "is_fund filter not supported in offline mode (is_fund data not stored)"
                )

            async with get_session() as session:
                # Start with companies joined with quotes
                stmt = select(Company, StockQuote).outerjoin(
                    StockQuote, Company.symbol == StockQuote.symbol
                )

                # Apply filters
                if sector:
                    stmt = stmt.where(func.upper(Company.sector) == sector.upper())
                if industry:
                    stmt = stmt.where(func.upper(Company.industry) == industry.upper())
                if country:
                    stmt = stmt.where(func.upper(Company.country) == country.upper())
                if exchange:
                    stmt = stmt.where(func.upper(Company.exchange_short_name) == exchange.upper())
                if is_etf is not None:
                    stmt = stmt.where(Company.is_etf == is_etf)
                if is_actively_trading is not None:
                    stmt = stmt.where(Company.is_actively_trading == is_actively_trading)

                # Quote-based filters
                if market_cap_more_than is not None:
                    stmt = stmt.where(StockQuote.market_cap >= market_cap_more_than)
                if market_cap_lower_than is not None:
                    stmt = stmt.where(StockQuote.market_cap <= market_cap_lower_than)
                if price_more_than is not None:
                    stmt = stmt.where(StockQuote.price >= price_more_than)
                if price_lower_than is not None:
                    stmt = stmt.where(StockQuote.price <= price_lower_than)
                if volume_more_than is not None:
                    stmt = stmt.where(StockQuote.volume >= volume_more_than)
                if volume_lower_than is not None:
                    stmt = stmt.where(StockQuote.volume <= volume_lower_than)

                result = await session.execute(stmt)
                rows = result.fetchall()

                data = []
                for company, quote in rows:
                    item = {
                        "symbol": company.symbol,
                        "companyName": company.company_name,
                        "sector": company.sector,
                        "industry": company.industry,
                        "country": company.country,
                        "exchange": company.exchange_short_name,
                        "isEtf": company.is_etf,
                        "isActivelyTrading": company.is_actively_trading,
                    }
                    if quote:
                        item.update(
                            {
                                "price": quote.price,
                                "marketCap": quote.market_cap,
                                "volume": quote.volume,
                            }
                        )
                    data.append(item)

                if limit:
                    data = data[:limit]

                response: dict[str, Any] = {
                    "data": data,
                    "count": len(data),
                    "error": None,
                }

                # Include warning about unsupported filters if any were provided
                if unsupported_filters:
                    response["_warning"] = (
                        f"The following filters are not supported in offline mode and were "
                        f"ignored: {', '.join(unsupported_filters)}. Use online mode for full "
                        f"filter support."
                    )

                return self._add_metadata(response)

        except Exception as e:
            logger.error(f"Database error in screen_stocks: {e}")
            return self._add_metadata(database_error(str(e)))

    # =========================================================================
    # Company Information (company_information.py) - 24 methods
    # =========================================================================

    async def get_company_profile(self, symbol: str) -> dict[str, Any]:
        """Get comprehensive company profile."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                result = await session.execute(
                    select(Company).where(Company.symbol == symbol.upper())
                )
                company = result.scalar_one_or_none()

                if company:
                    # Try to use stored profile data first
                    profile_data = self._parse_json_field(company.profile_data)
                    if not profile_data:
                        profile_data = {
                            "symbol": company.symbol,
                            "companyName": company.company_name,
                            "exchange": company.exchange,
                            "exchangeShortName": company.exchange_short_name,
                            "currency": company.currency,
                            "country": company.country,
                            "sector": company.sector,
                            "industry": company.industry,
                            "cik": company.cik,
                            "cusip": company.cusip,
                            "isin": company.isin,
                            "description": company.description,
                            "website": company.website,
                            "image": company.logo,
                            "ceo": company.ceo,
                            "fullTimeEmployees": company.full_time_employees,
                            "ipoDate": company.ipo_date,
                            "address": company.address,
                            "city": company.city,
                            "state": company.state,
                            "zip": company.zip,
                            "phone": company.phone,
                            "isEtf": company.is_etf,
                            "isActivelyTrading": company.is_actively_trading,
                        }

                    return self._add_metadata(
                        {
                            "symbol": symbol.upper(),
                            "profile": profile_data,
                            "error": None,
                        }
                    )

                return self._add_metadata(data_not_available("profile", symbol))

        except Exception as e:
            logger.error(f"Database error in get_company_profile: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_company_notes(self, symbol: str) -> dict[str, Any]:
        """Get company notes and filings."""
        return await self._get_generic_response(
            "/company-notes", {"symbol": symbol.upper()}, symbol
        )

    async def get_company_executives(
        self, symbol: str, active: bool | None = None
    ) -> dict[str, Any]:
        """Get company executive information."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if active is not None:
            params["active"] = active
        return await self._get_generic_response("/key-executives", params, symbol)

    async def get_executive_compensation(self, symbol: str) -> dict[str, Any]:
        """Get executive compensation data."""
        return await self._get_generic_response(
            "/governance/executive-compensation", {"symbol": symbol.upper()}, symbol
        )

    async def get_executive_comp_benchmark(self, year: int | None = None) -> dict[str, Any]:
        """Get executive compensation benchmarks."""
        params: dict[str, Any] = {}
        if year:
            params["year"] = year
        return await self._get_generic_response("/executive-compensation-benchmark", params)

    async def get_company_dividends(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company dividend history."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/historical-price-eod/dividend", params, symbol)

    async def get_company_earnings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company earnings data."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/historical/earning_calendar", params, symbol)

    async def get_stock_splits(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get stock split history."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/historical-price-eod/stock_split", params, symbol)

    async def get_stock_peers(self, symbol: str) -> dict[str, Any]:
        """Get peer companies for a stock."""
        return await self._get_generic_response("/stock_peers", {"symbol": symbol.upper()}, symbol)

    async def get_market_cap(self, symbol: str) -> dict[str, Any]:
        """Get current market capitalization."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol == symbol.upper())
                )
                quote = result.scalar_one_or_none()

                if quote and quote.market_cap:
                    return self._add_metadata(
                        {
                            "symbol": symbol.upper(),
                            "data": [
                                {
                                    "symbol": symbol.upper(),
                                    "marketCap": quote.market_cap,
                                }
                            ],
                            "error": None,
                        }
                    )

                return self._add_metadata(data_not_available("market cap", symbol))

        except Exception as e:
            logger.error(f"Database error in get_market_cap: {e}")
            return self._add_metadata(database_error(str(e)))

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
        return await self._get_generic_response("/historical-market-capitalization", params, symbol)

    async def get_batch_market_cap(self, symbols: str) -> dict[str, Any]:
        """Get market cap for multiple symbols."""
        try:
            symbol_list = [s.strip().upper() for s in symbols.split(",")]
            async with get_session() as session:
                result = await session.execute(
                    select(StockQuote).where(StockQuote.symbol.in_(symbol_list))
                )
                quotes = result.scalars().all()

                data = [
                    {"symbol": q.symbol, "marketCap": q.market_cap} for q in quotes if q.market_cap
                ]

                return self._add_metadata(
                    {
                        "symbols": symbols,
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_batch_market_cap: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_shares_float(self, symbol: str) -> dict[str, Any]:
        """Get shares float data."""
        return await self._get_generic_response("/shares_float", {"symbol": symbol.upper()}, symbol)

    async def get_all_shares_float(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get shares float for all companies."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/shares_float/all", params)

    async def get_employee_count(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company employee count."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                result = await session.execute(
                    select(Company).where(Company.symbol == symbol.upper())
                )
                company = result.scalar_one_or_none()

                if company and company.full_time_employees:
                    return self._add_metadata(
                        {
                            "symbol": symbol.upper(),
                            "data": [
                                {
                                    "symbol": symbol.upper(),
                                    "employeeCount": company.full_time_employees,
                                    "companyName": company.company_name,
                                }
                            ],
                            "error": None,
                        }
                    )

                return self._add_metadata(data_not_available("employee count", symbol))

        except Exception as e:
            logger.error(f"Database error in get_employee_count: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_historical_employee_count(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get historical employee count."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/historical/employee_count", params, symbol)

    async def get_stock_grades(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get analyst stock grades."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/grade", params, symbol)

    async def get_revenue_by_geography(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get revenue breakdown by geography."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if period:
            params["period"] = period
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/revenue-geographic-segmentation", params, symbol)

    async def get_revenue_by_product(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get revenue breakdown by product."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if period:
            params["period"] = period
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/revenue-product-segmentation", params, symbol)

    async def get_profile_by_cik(self, cik: str) -> dict[str, Any]:
        """Get company profile by CIK."""
        try:
            async with get_session() as session:
                result = await session.execute(select(Company).where(Company.cik == cik))
                company = result.scalar_one_or_none()

                if company:
                    profile_data = self._parse_json_field(company.profile_data)
                    if not profile_data:
                        profile_data = {
                            "symbol": company.symbol,
                            "companyName": company.company_name,
                            "cik": company.cik,
                        }
                    return self._add_metadata(
                        {
                            "cik": cik,
                            "profile": profile_data,
                            "error": None,
                        }
                    )

                return self._add_metadata(data_not_available("company with CIK", cik))

        except Exception as e:
            logger.error(f"Database error in get_profile_by_cik: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_enterprise_values(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get enterprise value data."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if period:
            params["period"] = period
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/enterprise-values", params, symbol)

    async def get_ratings_snapshot(self, symbol: str) -> dict[str, Any]:
        """Get current analyst ratings snapshot."""
        return await self._get_generic_response("/rating", {"symbol": symbol.upper()}, symbol)

    async def get_ratings_historical(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get historical analyst ratings."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/historical-rating", params, symbol)

    async def get_owner_earnings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get owner earnings data."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/owner-earnings", params, symbol)

    # =========================================================================
    # Financial Statements (financial_statements.py) - 27 methods
    # =========================================================================

    async def _get_financial_statement(
        self,
        symbol: str,
        statement_type: str,
        period: str = "annual",
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Helper to get financial statements from database."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                stmt = (
                    select(FinancialStatement)
                    .where(
                        FinancialStatement.symbol == symbol.upper(),
                        FinancialStatement.statement_type == statement_type,
                        FinancialStatement.period == period,
                    )
                    .order_by(FinancialStatement.fiscal_date.desc())
                )

                if limit:
                    stmt = stmt.limit(limit)

                result = await session.execute(stmt)
                statements = result.scalars().all()

                data = [self._parse_json_field(s.statement_data) for s in statements]

                error = None
                if not data:
                    error = (
                        f"No {statement_type} data available for {symbol.upper()}. "
                        "The symbol may not have financial statements in the database, "
                        "or the requested period may not be available."
                    )

                return self._add_metadata(
                    {
                        "symbol": symbol.upper(),
                        "data": data,
                        "count": len(data),
                        "error": error,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in _get_financial_statement: {e}")
            return self._add_metadata(database_error(str(e)))

    async def _get_ttm_statement(self, symbol: str, statement_type: str) -> dict[str, Any]:
        """Helper to get TTM financial statement."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                # Use .first() instead of .scalar_one_or_none() because multiple
                # records may exist with same symbol+statement_type but different periods.
                # Order by fiscal_date desc to get the most recent TTM data.
                result = await session.execute(
                    select(FinancialStatement)
                    .where(
                        FinancialStatement.symbol == symbol.upper(),
                        FinancialStatement.statement_type == statement_type,
                    )
                    .order_by(FinancialStatement.fiscal_date.desc())
                )
                statement = result.scalars().first()

                if statement:
                    data = self._parse_json_field(statement.statement_data)
                    return self._add_metadata(
                        {
                            "symbol": symbol.upper(),
                            "data": [data] if data else [],
                            "error": None,
                        }
                    )

                return self._add_metadata(data_not_available(f"{statement_type} data", symbol))

        except Exception as e:
            logger.error(f"Database error in _get_ttm_statement: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_income_statement(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement data."""
        return await self._get_financial_statement(symbol, "income", period, limit)

    async def get_balance_sheet(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet data."""
        return await self._get_financial_statement(symbol, "balance_sheet", period, limit)

    async def get_cash_flow_statement(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow statement data."""
        return await self._get_financial_statement(symbol, "cash_flow", period, limit)

    async def get_latest_financials(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest financial statements across companies."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/latest-financial-statements", params)

    async def get_stocks_with_financials(self, page: int = 0) -> dict[str, Any]:
        """Get list of stocks with financial data."""
        try:
            async with get_session() as session:
                # Get distinct symbols from financial statements
                result = await session.execute(select(FinancialStatement.symbol).distinct())
                symbols = [row[0] for row in result.fetchall()]

                # Paginate
                page_size = 100
                start = page * page_size
                end = start + page_size
                page_symbols = symbols[start:end]

                return self._add_metadata(
                    {
                        "data": page_symbols,
                        "page": page,
                        "count": len(page_symbols),
                        "total": len(symbols),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_stocks_with_financials: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_income_statement_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months income statement."""
        return await self._get_ttm_statement(symbol, "income_ttm")

    async def get_balance_sheet_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months balance sheet."""
        return await self._get_ttm_statement(symbol, "balance_sheet_ttm")

    async def get_cash_flow_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months cash flow."""
        return await self._get_ttm_statement(symbol, "cash_flow_ttm")

    async def get_income_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/income-statement-as-reported", params, symbol)

    async def get_balance_sheet_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response(
            "/balance-sheet-statement-as-reported", params, symbol
        )

    async def get_cash_flow_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow statement as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/cash-flow-statement-as-reported", params, symbol)

    async def get_full_financials_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get full financial statements as reported."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "period": period}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response(
            "/financial-statement-full-as-reported", params, symbol
        )

    async def get_financial_report_json(
        self, symbol: str, year: int, period: str = "FY"
    ) -> dict[str, Any]:
        """Get financial report in JSON format."""
        return await self._get_generic_response(
            "/financial-reports-json",
            {"symbol": symbol.upper(), "year": year, "period": period},
            symbol,
        )

    async def get_financial_report_xlsx(
        self, symbol: str, year: int, period: str = "FY"
    ) -> dict[str, Any]:
        """Get financial report in XLSX format."""
        # XLSX export is not supported in offline mode
        return self._add_metadata(not_supported("XLSX financial report export"))

    async def get_financial_reports_dates(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get available financial report dates."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/financial-reports-dates", params, symbol)

    async def get_key_metrics(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get key financial metrics."""
        return await self._get_financial_statement(symbol, "key_metrics", period, limit)

    async def get_key_metrics_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months key metrics."""
        return await self._get_ttm_statement(symbol, "key_metrics_ttm")

    async def get_financial_ratios(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get financial ratios."""
        return await self._get_financial_statement(symbol, "ratios", period, limit)

    async def get_ratios_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months ratios."""
        return await self._get_ttm_statement(symbol, "ratios_ttm")

    async def get_financial_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get financial growth metrics."""
        return await self._get_financial_statement(symbol, "growth", period, limit)

    async def get_income_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement growth metrics."""
        return await self._get_financial_statement(symbol, "income_growth", period, limit)

    async def get_balance_sheet_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet growth metrics."""
        return await self._get_financial_statement(symbol, "balance_sheet_growth", period, limit)

    async def get_cash_flow_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow growth metrics."""
        return await self._get_financial_statement(symbol, "cash_flow_growth", period, limit)

    async def get_financial_scores(self, symbol: str) -> dict[str, Any]:
        """Get financial health scores."""
        return await self._get_ttm_statement(symbol, "scores")

    # =========================================================================
    # Technical Indicators (technical_indicators.py) - 10 methods
    # =========================================================================

    async def _get_technical_indicator(
        self,
        symbol: str,
        indicator_type: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Helper to get technical indicators from database."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                query = select(TechnicalIndicator).where(
                    TechnicalIndicator.symbol == symbol.upper(),
                    TechnicalIndicator.indicator_type == indicator_type,
                    TechnicalIndicator.period == period,
                    TechnicalIndicator.interval == interval,
                )
                if from_date:
                    query = query.where(TechnicalIndicator.date >= from_date)
                if to_date:
                    query = query.where(TechnicalIndicator.date <= to_date)
                query = query.order_by(TechnicalIndicator.date.desc())

                result = await session.execute(query)
                indicators = result.scalars().all()

                data = []
                for ind in indicators:
                    ind_data = self._parse_json_field(ind.indicator_data)
                    if not ind_data:
                        ind_data = {
                            "date": ind.date.isoformat() if ind.date else None,
                            indicator_type: ind.value,
                            "open": ind.open,
                            "high": ind.high,
                            "low": ind.low,
                            "close": ind.close,
                            "volume": ind.volume,
                        }
                    data.append(ind_data)

                return self._add_metadata(
                    {
                        "symbol": symbol.upper(),
                        "indicator": indicator_type,
                        "period": period,
                        "interval": interval,
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in _get_technical_indicator: {e}")
            return self._add_metadata(database_error(str(e)))

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
            symbol, "sma", period, interval, from_date, to_date
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
            symbol, "ema", period, interval, from_date, to_date
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
            symbol, "wma", period, interval, from_date, to_date
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
            symbol, "dema", period, interval, from_date, to_date
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
            symbol, "tema", period, interval, from_date, to_date
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
            symbol, "williams", period, interval, from_date, to_date
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
            symbol, "rsi", period, interval, from_date, to_date
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
            symbol, "adx", period, interval, from_date, to_date
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
            symbol, "standard_deviation", period, interval, from_date, to_date
        )

    # =========================================================================
    # Charts / Historical Prices (charts.py) - 11 methods
    # =========================================================================

    async def _get_historical_prices(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        light: bool = False,
    ) -> dict[str, Any]:
        """Helper to get historical prices from database."""
        try:
            async with get_session() as session:
                if error := await self._check_symbol_available(session, symbol):
                    return self._add_metadata(error)

                stmt = (
                    select(HistoricalPrice)
                    .where(HistoricalPrice.symbol == symbol.upper())
                    .order_by(HistoricalPrice.date.desc())
                )

                if from_date:
                    stmt = stmt.where(HistoricalPrice.date >= date.fromisoformat(from_date))
                if to_date:
                    stmt = stmt.where(HistoricalPrice.date <= date.fromisoformat(to_date))

                result = await session.execute(stmt)
                prices = result.scalars().all()

                if light:
                    # Match FMP API /historical-price-eod/light format
                    # which returns 'price' (not 'close') and 'symbol'
                    data = [
                        {
                            "symbol": symbol.upper(),
                            "date": p.date.isoformat() if p.date else None,
                            "price": p.close,  # FMP light endpoint uses 'price' not 'close'
                            "volume": p.volume,
                        }
                        for p in prices
                    ]
                else:
                    data = []
                    for p in prices:
                        price_data = self._parse_json_field(p.price_data)
                        if not price_data:
                            price_data = {
                                "date": p.date.isoformat() if p.date else None,
                                "open": p.open,
                                "high": p.high,
                                "low": p.low,
                                "close": p.close,
                                "adjOpen": p.adj_open,
                                "adjHigh": p.adj_high,
                                "adjLow": p.adj_low,
                                "adjClose": p.adj_close,
                                "volume": p.volume,
                                "unadjustedVolume": p.unadjusted_volume,
                                "change": p.change,
                                "changePercent": p.change_percent,
                                "vwap": p.vwap,
                                "label": p.label,
                                "changeOverTime": p.change_over_time,
                            }
                        else:
                            # Ensure all fields are present even when using stored JSON
                            # Database columns are authoritative - fill in any missing fields
                            field_column_mappings = [
                                ("date", lambda: p.date.isoformat() if p.date else None),
                                ("open", lambda: p.open),
                                ("high", lambda: p.high),
                                ("low", lambda: p.low),
                                ("close", lambda: p.close),
                                ("adjOpen", lambda: p.adj_open),
                                ("adjHigh", lambda: p.adj_high),
                                ("adjLow", lambda: p.adj_low),
                                ("adjClose", lambda: p.adj_close),
                                ("volume", lambda: p.volume),
                                ("unadjustedVolume", lambda: p.unadjusted_volume),
                                ("change", lambda: p.change),
                                ("changePercent", lambda: p.change_percent),
                                ("vwap", lambda: p.vwap),
                                ("label", lambda: p.label),
                                ("changeOverTime", lambda: p.change_over_time),
                            ]
                            for field_name, get_value in field_column_mappings:
                                if field_name not in price_data:
                                    value = get_value()
                                    if value is not None:
                                        price_data[field_name] = value
                        data.append(price_data)

                error = None
                if not data:
                    date_range_msg = ""
                    if from_date and to_date:
                        date_range_msg = f" for the period {from_date} to {to_date}"
                    elif from_date:
                        date_range_msg = f" from {from_date}"
                    elif to_date:
                        date_range_msg = f" up to {to_date}"
                    error = (
                        f"No historical price data available for {symbol.upper()}{date_range_msg}. "
                        "The symbol may not be in the database or the date range may have no data."
                    )

                return self._add_metadata(
                    {
                        "symbol": symbol.upper(),
                        "historical": data,
                        "count": len(data),
                        "error": error,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in _get_historical_prices: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_historical_price_full(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get full historical price data."""
        return await self._get_historical_prices(symbol, from_date, to_date, light=False)

    async def get_historical_price_light(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get lightweight split-adjusted historical price data."""
        return await self._get_historical_prices(symbol, from_date, to_date, light=True)

    async def get_historical_price_dividend_adjusted(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get fully-adjusted historical prices (split-adjusted and dividend-adjusted).

        Returns ONLY fully adjusted fields (adjOpen, adjHigh, adjLow, adjClose)
        to match the FMP API /historical-price-eod/dividend-adjusted response format.
        This ensures consistency between online and offline modes.
        """
        result = await self._get_historical_prices(symbol, from_date, to_date, light=False)

        # Transform to match FMP dividend-adjusted endpoint format
        # which returns ONLY adjusted fields, not regular OHLC
        if "historical" in result and isinstance(result["historical"], list):
            transformed = []
            for record in result["historical"]:
                transformed.append(
                    {
                        "symbol": symbol.upper(),
                        "date": record.get("date"),
                        "adjOpen": record.get("adjOpen"),
                        "adjHigh": record.get("adjHigh"),
                        "adjLow": record.get("adjLow"),
                        "adjClose": record.get("adjClose"),
                        "volume": record.get("volume"),
                    }
                )
            result["historical"] = transformed

        return result

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
        result = await self._get_historical_prices(symbol, from_date, to_date, light=False)

        if "historical" in result and isinstance(result["historical"], list):
            result["_warning"] = (
                "Note: Despite the function name, this returns split-adjusted prices. "
                "FMP's /historical-price-eod/full endpoint does not provide truly unadjusted "
                "daily data. For pre-split prices, use intraday endpoints with nonadjusted=true."
            )

        return result

    async def get_intraday_1min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 1-minute intraday data."""
        # Intraday data typically not stored in offline mode
        params: dict[str, Any] = {"symbol": symbol.upper(), "from": from_date, "to": to_date}
        if nonadjusted is not None:
            params["nonadjusted"] = nonadjusted
        return await self._get_generic_response(
            "/historical-chart/1min",
            params,
            symbol,
        )

    async def get_intraday_5min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 5-minute intraday data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "from": from_date, "to": to_date}
        if nonadjusted is not None:
            params["nonadjusted"] = nonadjusted
        return await self._get_generic_response(
            "/historical-chart/5min",
            params,
            symbol,
        )

    async def get_intraday_15min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 15-minute intraday data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "from": from_date, "to": to_date}
        if nonadjusted is not None:
            params["nonadjusted"] = nonadjusted
        return await self._get_generic_response(
            "/historical-chart/15min",
            params,
            symbol,
        )

    async def get_intraday_30min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 30-minute intraday data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "from": from_date, "to": to_date}
        if nonadjusted is not None:
            params["nonadjusted"] = nonadjusted
        return await self._get_generic_response(
            "/historical-chart/30min",
            params,
            symbol,
        )

    async def get_intraday_1hour(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 1-hour intraday data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "from": from_date, "to": to_date}
        if nonadjusted is not None:
            params["nonadjusted"] = nonadjusted
        return await self._get_generic_response(
            "/historical-chart/1hour",
            params,
            symbol,
        )

    async def get_intraday_4hour(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 4-hour intraday data."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "from": from_date, "to": to_date}
        if nonadjusted is not None:
            params["nonadjusted"] = nonadjusted
        return await self._get_generic_response(
            "/historical-chart/4hour",
            params,
            symbol,
        )

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
        return await self._get_generic_response("/analyst-estimates", params, symbol)

    async def get_grades_consensus(self, symbol: str) -> dict[str, Any]:
        """Get analyst grades consensus."""
        return await self._get_generic_response(
            "/grades-consensus", {"symbol": symbol.upper()}, symbol
        )

    async def get_grades_historical(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get historical analyst grades."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/grades-historical", params, symbol)

    async def get_price_target_consensus(self, symbol: str) -> dict[str, Any]:
        """Get price target consensus."""
        return await self._get_generic_response(
            "/price-target-consensus", {"symbol": symbol.upper()}, symbol
        )

    async def get_price_target_summary(self, symbol: str) -> dict[str, Any]:
        """Get price target summary."""
        return await self._get_generic_response(
            "/price-target-summary", {"symbol": symbol.upper()}, symbol
        )

    async def get_price_target_news(
        self, symbol: str, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get price target news for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/price-target-rss-feed", params, symbol)

    async def get_price_target_latest_news(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest price target news."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/price-target-latest-news", params)

    async def get_grade_news(
        self, symbol: str, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get grade news for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper(), "page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/upgrades-downgrades-rss-feed", params, symbol)

    async def get_grade_latest_news(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest grade news."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/upgrades-downgrades-rss-feed", params)

    async def get_latest_mergers_acquisitions(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest mergers & acquisitions."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/mergers-acquisitions-rss-feed", params)

    async def search_ma(self, query: str) -> dict[str, Any]:
        """Search for M&A deals by company name."""
        return await self._get_generic_response("/mergers-acquisitions-search", {"name": query})

    # =========================================================================
    # News (news.py) - 10 methods
    # =========================================================================

    async def get_fmp_articles(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get FMP articles."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/fmp/articles", params)

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
        return await self._get_generic_response("/news/general-latest", params)

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
        return await self._get_generic_response("/news/stock-latest", params)

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
        return await self._get_generic_response("/news/stock", params, symbol)

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
        return await self._get_generic_response("/news/forex-latest", params)

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
        return await self._get_generic_response("/news/forex", params, symbol)

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
        return await self._get_generic_response("/news/crypto-latest", params)

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
        return await self._get_generic_response("/news/crypto", params, symbol)

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
        return await self._get_generic_response("/press-releases-latest", params)

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
        return await self._get_generic_response("/press-releases", params, symbol)

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
        return await self._get_generic_response("/sectors-performance", params)

    async def get_industry_performance_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        industry: str | None = None,
    ) -> dict[str, Any]:
        """Get industry performance snapshot."""
        params: dict[str, Any] = {"date": date}
        if exchange:
            params["exchange"] = exchange
        if industry:
            params["industry"] = industry
        return await self._get_generic_response("/industries-performance", params)

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
        return await self._get_generic_response("/historical-sectors-performance", params)

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
        return await self._get_generic_response("/historical-industries-performance", params)

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
        return await self._get_generic_response("/sector_price_earning_ratio", params)

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
        return await self._get_generic_response("/industry_price_earning_ratio", params)

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
        return await self._get_generic_response("/historical-sector-price-earning-ratio", params)

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
        return await self._get_generic_response("/historical-industry-price-earning-ratio", params)

    async def get_biggest_gainers(self) -> dict[str, Any]:
        """Get biggest gainers."""
        return await self._get_generic_response("/biggest-gainers", {})

    async def get_biggest_losers(self) -> dict[str, Any]:
        """Get biggest losers."""
        return await self._get_generic_response("/biggest-losers", {})

    async def get_most_actives(self) -> dict[str, Any]:
        """Get most active stocks."""
        return await self._get_generic_response("/most-actives", {})

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
        return await self._get_generic_response("/earning_calendar", params)

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
        return await self._get_generic_response("/stock_dividend_calendar", params)

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
        return await self._get_generic_response("/stock_split_calendar", params)

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
        return await self._get_generic_response("/ipo_calendar", params)

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
        return await self._get_generic_response("/ipo-calendar-prospectus", params)

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
        return await self._get_generic_response("/ipo-calendar-confirmed", params)

    # =========================================================================
    # Earnings Transcript (earnings_transcript.py) - 5 methods
    # =========================================================================

    async def get_earning_call_transcript(
        self, symbol: str, year: int, quarter: int
    ) -> dict[str, Any]:
        """Get earnings call transcript."""
        return await self._get_generic_response(
            "/earning_call_transcript",
            {"symbol": symbol.upper(), "year": year, "quarter": quarter},
            symbol,
        )

    async def get_transcript_dates_by_symbol(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get available transcript dates for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/earning_call_transcript/dates", params, symbol)

    async def get_companies_with_transcripts(self, limit: int | None = None) -> dict[str, Any]:
        """Get companies with earnings transcripts."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/earning_call_transcript/companies", params)

    async def get_latest_earning_transcripts(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest earnings transcripts."""
        params: dict[str, Any] = {"page": page}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/earning_call_transcript/latest", params)

    # =========================================================================
    # DCF Valuation (dcf_valuation.py) - 4 methods
    # =========================================================================

    async def get_dcf_valuation(self, symbol: str) -> dict[str, Any]:
        """Get DCF valuation."""
        return await self._get_generic_response(
            "/discounted-cash-flow", {"symbol": symbol.upper()}, symbol
        )

    async def get_levered_dcf_valuation(self, symbol: str) -> dict[str, Any]:
        """Get levered DCF valuation."""
        return await self._get_generic_response(
            "/advanced-discounted-cash-flow", {"symbol": symbol.upper()}, symbol
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
        # Custom DCF is not supported in offline mode
        return self._add_metadata(not_supported("Custom DCF valuation"))

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
        # Custom DCF is not supported in offline mode
        return self._add_metadata(not_supported("Custom levered DCF valuation"))

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
        return await self._get_generic_response("/treasury", params)

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
        return await self._get_generic_response("/economic", params)

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
        return await self._get_generic_response("/economic_calendar", params)

    async def get_market_risk_premium(self) -> dict[str, Any]:
        """Get market risk premium."""
        return await self._get_generic_response("/market_risk_premium", {})

    # =========================================================================
    # ETF/Mutual Funds (etf_mutual_funds.py) - 9 methods
    # =========================================================================

    async def get_etf_holdings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get ETF holdings."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/etf-holdings", params, symbol)

    async def get_etf_info(self, symbol: str) -> dict[str, Any]:
        """Get ETF information."""
        return await self._get_generic_response("/etf-info", {"symbol": symbol.upper()}, symbol)

    async def get_etf_sector_weightings(self, symbol: str) -> dict[str, Any]:
        """Get ETF sector weightings."""
        return await self._get_generic_response(
            "/etf-sector-weightings", {"symbol": symbol.upper()}, symbol
        )

    async def get_etf_country_weightings(self, symbol: str) -> dict[str, Any]:
        """Get ETF country weightings."""
        return await self._get_generic_response(
            "/etf-country-weightings", {"symbol": symbol.upper()}, symbol
        )

    async def get_etf_asset_exposure(self, symbol: str) -> dict[str, Any]:
        """Get ETF asset class exposure."""
        return await self._get_generic_response(
            "/etf-stock-exposure", {"symbol": symbol.upper()}, symbol
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
        return await self._get_generic_response("/mutual-fund-holdings", params, symbol)

    async def get_fund_disclosure_dates(
        self, symbol: str, cik: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get fund disclosure dates."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if cik:
            params["cik"] = cik
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/mutual-fund-holdings/dates", params, symbol)

    async def get_fund_disclosure_holders_latest(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest fund disclosure holders."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/mutual-fund-holdings/latest", params, symbol)

    async def search_fund_disclosure_by_name(
        self, name: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Search fund disclosures by name."""
        params: dict[str, Any] = {"name": name}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/mutual-fund-holdings/search", params)

    # =========================================================================
    # Stock Directory (stock_directory.py) - 14 methods
    # =========================================================================

    async def get_all_stock_symbols(self, limit: int | None = None) -> dict[str, Any]:
        """Get all stock symbols."""
        try:
            async with get_session() as session:
                # Include companies where is_etf is False OR is_etf is NULL
                # (NULL != True evaluates to NULL in SQL, which excludes those rows)
                result = await session.execute(
                    select(Company).where(
                        or_(Company.is_etf == False, Company.is_etf.is_(None))  # noqa: E712
                    )
                )
                companies = result.scalars().all()

                data = [
                    {
                        "symbol": c.symbol,
                        "name": c.company_name,
                        "exchange": c.exchange_short_name,
                        "exchangeShortName": c.exchange_short_name,
                    }
                    for c in companies
                ]

                if limit:
                    data = data[:limit]

                return self._add_metadata(
                    {
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_all_stock_symbols: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_all_etfs(self, limit: int | None = None) -> dict[str, Any]:
        """Get all ETF symbols."""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Company).where(Company.is_etf == True)  # noqa: E712
                )
                companies = result.scalars().all()

                data = [
                    {
                        "symbol": c.symbol,
                        "name": c.company_name,
                        "exchange": c.exchange_short_name,
                    }
                    for c in companies
                ]

                if limit:
                    data = data[:limit]

                return self._add_metadata(
                    {
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_all_etfs: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_supported_exchanges(self) -> dict[str, Any]:
        """Get list of supported exchanges."""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Company.exchange_short_name)
                    .distinct()
                    .where(Company.exchange_short_name.isnot(None))
                )
                exchanges = [row[0] for row in result.fetchall() if row[0]]

                return self._add_metadata(
                    {
                        "data": exchanges,
                        "count": len(exchanges),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_supported_exchanges: {e}")
            return self._add_metadata(database_error(str(e)))

    async def find_exchange_listings(
        self, exchange: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Find listings for an exchange."""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Company).where(
                        func.upper(Company.exchange_short_name) == exchange.upper()
                    )
                )
                companies = result.scalars().all()

                data = [
                    {
                        "symbol": c.symbol,
                        "name": c.company_name,
                        "exchange": c.exchange_short_name,
                    }
                    for c in companies
                ]

                if limit:
                    data = data[:limit]

                return self._add_metadata(
                    {
                        "exchange": exchange,
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in find_exchange_listings: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_actively_trading_stocks(self, limit: int | None = None) -> dict[str, Any]:
        """Get actively trading stocks."""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Company).where(
                        Company.is_actively_trading == True  # noqa: E712
                    )
                )
                companies = result.scalars().all()

                data = [
                    {
                        "symbol": c.symbol,
                        "name": c.company_name,
                        "exchange": c.exchange_short_name,
                    }
                    for c in companies
                ]

                if limit:
                    data = data[:limit]

                return self._add_metadata(
                    {
                        "data": data,
                        "count": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_actively_trading_stocks: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_delisted_companies(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get delisted companies."""
        return await self._get_generic_response(
            "/delisted-companies", {"page": page, "limit": limit}
        )

    async def get_cik_database(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get CIK database."""
        try:
            async with get_session() as session:
                result = await session.execute(select(Company).where(Company.cik.isnot(None)))
                companies = result.scalars().all()

                data = [
                    {"symbol": c.symbol, "name": c.company_name, "cik": c.cik}
                    for c in companies
                    if c.cik
                ]

                # Paginate
                page_size = limit if limit is not None else 100
                start = page * page_size
                end = start + page_size
                page_data = data[start:end]

                return self._add_metadata(
                    {
                        "data": page_data,
                        "page": page,
                        "count": len(page_data),
                        "total": len(data),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_cik_database: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_all_countries(self) -> dict[str, Any]:
        """Get all countries."""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Company.country).distinct().where(Company.country.isnot(None))
                )
                countries = [row[0] for row in result.fetchall() if row[0]]

                return self._add_metadata(
                    {
                        "data": sorted(countries),
                        "count": len(countries),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_all_countries: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_all_sectors(self) -> dict[str, Any]:
        """Get all sectors."""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Company.sector).distinct().where(Company.sector.isnot(None))
                )
                sectors = [row[0] for row in result.fetchall() if row[0]]

                return self._add_metadata(
                    {
                        "data": sorted(sectors),
                        "count": len(sectors),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_all_sectors: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_all_industries(self) -> dict[str, Any]:
        """Get all industries."""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Company.industry).distinct().where(Company.industry.isnot(None))
                )
                industries = [row[0] for row in result.fetchall() if row[0]]

                return self._add_metadata(
                    {
                        "data": sorted(industries),
                        "count": len(industries),
                        "error": None,
                    }
                )

        except Exception as e:
            logger.error(f"Database error in get_all_industries: {e}")
            return self._add_metadata(database_error(str(e)))

    async def get_recent_symbol_changes(
        self, invalid: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get recent symbol changes."""
        params: dict[str, Any] = {}
        if invalid is not None:
            params["invalid"] = invalid
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/symbol_change", params)

    async def get_cryptocurrency_list(self, limit: int | None = None) -> dict[str, Any]:
        """Get cryptocurrency list."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/cryptocurrencies-list", params)

    async def get_forex_currency_pairs(self, limit: int | None = None) -> dict[str, Any]:
        """Get forex currency pairs."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/forex-pairs-list", params)

    async def get_commodities_list(self, limit: int | None = None) -> dict[str, Any]:
        """Get commodities list."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/commodities-list", params)

    # =========================================================================
    # Indexes (indexes.py) - 7 methods
    # =========================================================================

    async def get_index_list(self) -> dict[str, Any]:
        """Get list of available indexes."""
        return await self._get_generic_response("/index-list", {})

    async def get_sp500_constituents(self, limit: int | None = None) -> dict[str, Any]:
        """Get S&P 500 constituents."""
        result = await self._get_generic_response("/sp500_constituent", {})
        if limit is not None and isinstance(result.get("data"), list):
            result = dict(result)
            result["data"] = result["data"][:limit]
            result["count"] = len(result["data"])
        return result

    async def get_nasdaq_constituents(self) -> dict[str, Any]:
        """Get NASDAQ constituents."""
        return await self._get_generic_response("/nasdaq_constituent", {})

    async def get_dowjones_constituents(self) -> dict[str, Any]:
        """Get Dow Jones constituents."""
        return await self._get_generic_response("/dowjones_constituent", {})

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
        return await self._get_generic_response("/historical/sp500_constituent", params)

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
        return await self._get_generic_response("/historical/nasdaq_constituent", params)

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
        return await self._get_generic_response("/historical/dowjones_constituent", params)

    # =========================================================================
    # Market Hours (market_hours.py) - 3 methods
    # =========================================================================

    async def get_all_exchange_market_hours(self) -> dict[str, Any]:
        """Get market hours for all exchanges."""
        return await self._get_generic_response("/market-hours", {})

    async def get_exchange_market_hours(self, exchange: str) -> dict[str, Any]:
        """Get market hours for a specific exchange."""
        return await self._get_generic_response("/market-hours", {"exchange": exchange})

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
        return await self._get_generic_response("/market-holidays", params)

    # =========================================================================
    # Congressional Trading (congressional_trading.py) - 4 methods
    # =========================================================================

    async def get_house_disclosure(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get House of Representatives financial disclosures."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/house-disclosure", params)

    async def get_senate_disclosure(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get Senate financial disclosures."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/senate-disclosure", params)

    async def get_senate_trades(
        self, symbol: str, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get Senate trades for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/senate-trading", params)

    async def get_house_trades(
        self, symbol: str, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get House trades for a symbol."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/house-trading", params)

    # =========================================================================
    # SEC Filings (sec_filings.py) - 2 methods
    # =========================================================================

    async def get_institutional_ownership(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest institutional ownership filings."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/institutional-ownership", params)

    async def get_8k_filings(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest 8-K filings."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if limit:
            params["limit"] = limit
        return await self._get_generic_response("/sec-filings-8k", params)
