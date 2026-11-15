"""
Company search tools using provider pattern.

From FMP API:

search by symbol
search by company name
search by cik
search by cusip
search by isin
screen stocks
find exchange listings

"""

from mcp_servers.fmp_server.models import (
    ExchangeListingsRequest,
    ScreenStocksRequest,
    SearchByCikRequest,
    SearchByCompanyNameRequest,
    SearchByCusipRequest,
    SearchByIsinRequest,
    SearchBySymbolRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def search_by_symbol(request: SearchBySymbolRequest) -> dict:
    """Search for stock information by ticker symbol or partial symbol."""
    provider = get_provider()
    return await provider.search_by_symbol(request.query, request.limit, request.exchange)


async def search_by_company_name(request: SearchByCompanyNameRequest) -> dict:
    """Search for ticker symbols by company name or partial company name."""
    provider = get_provider()
    return await provider.search_by_company_name(request.query, request.limit, request.exchange)


async def search_by_cik(request: SearchByCikRequest) -> dict:
    """Search by CIK (Central Index Key) - SEC's unique identifier for companies."""
    provider = get_provider()
    return await provider.search_by_cik(request.cik)


async def search_by_cusip(request: SearchByCusipRequest) -> dict:
    """Search by CUSIP number - unique identifier for financial securities in US/Canada."""
    provider = get_provider()
    return await provider.search_by_cusip(request.cusip)


async def search_by_isin(request: SearchByIsinRequest) -> dict:
    """Search by ISIN (International Securities Identification Number)."""
    provider = get_provider()
    return await provider.search_by_isin(request.isin)


async def screen_stocks(request: ScreenStocksRequest) -> dict:
    """Screen/filter stocks based on multiple criteria."""
    provider = get_provider()
    return await provider.screen_stocks(
        market_cap_more_than=request.market_cap_more_than,
        market_cap_lower_than=request.market_cap_lower_than,
        price_more_than=request.price_more_than,
        price_lower_than=request.price_lower_than,
        beta_more_than=request.beta_more_than,
        beta_lower_than=request.beta_lower_than,
        volume_more_than=request.volume_more_than,
        volume_lower_than=request.volume_lower_than,
        dividend_more_than=request.dividend_more_than,
        dividend_lower_than=request.dividend_lower_than,
        is_etf=request.is_etf,
        is_fund=request.is_fund,
        is_actively_trading=request.is_actively_trading,
        sector=request.sector,
        industry=request.industry,
        country=request.country,
        exchange=request.exchange,
        limit=request.limit,
    )


async def find_exchange_listings(request: ExchangeListingsRequest) -> dict:
    """Find all stocks listed on a specific exchange."""
    provider = get_provider()
    return await provider.find_exchange_listings(request.exchange, request.limit)
