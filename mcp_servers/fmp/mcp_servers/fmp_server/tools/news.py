"""
News tools using provider pattern.

From FMP API:

fmp articles
general news
press releases
stock news
crypto news
forex news
search press releases by symbol
search stock news by symbol
search crypto news by symbol
search forex news by symbol

"""

from mcp_servers.fmp_server.models import (
    FmpArticlesRequest,
    NewsLatestRequest,
    NewsSearchBySymbolRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_fmp_articles(request: FmpArticlesRequest) -> dict:
    """Access the latest articles from Financial Modeling Prep."""
    provider = get_provider()
    return await provider.get_fmp_articles(request.page, request.limit)


async def get_general_news_latest(request: NewsLatestRequest) -> dict:
    """Access the latest general news articles from variety of sources."""
    provider = get_provider()
    return await provider.get_general_news_latest(
        request.page, request.limit, request.from_date, request.to_date
    )


async def get_press_releases_latest(request: NewsLatestRequest) -> dict:
    """Access official company press releases."""
    provider = get_provider()
    return await provider.get_press_releases_latest(
        request.page, request.limit, request.from_date, request.to_date
    )


async def get_stock_news_latest(request: NewsLatestRequest) -> dict:
    """Stay informed with latest stock market news."""
    provider = get_provider()
    return await provider.get_stock_news_latest(
        request.page, request.limit, request.from_date, request.to_date
    )


async def get_crypto_news_latest(request: NewsLatestRequest) -> dict:
    """Stay informed with latest cryptocurrency news."""
    provider = get_provider()
    return await provider.get_crypto_news_latest(
        request.page, request.limit, request.from_date, request.to_date
    )


async def get_forex_news_latest(request: NewsLatestRequest) -> dict:
    """Stay updated with latest forex news articles."""
    provider = get_provider()
    return await provider.get_forex_news_latest(
        request.page, request.limit, request.from_date, request.to_date
    )


async def search_press_releases_by_symbol(request: NewsSearchBySymbolRequest) -> dict:
    """Search for company press releases by stock symbol."""
    provider = get_provider()
    return await provider.search_press_releases_by_symbol(
        request.symbols, request.page, request.limit, request.from_date, request.to_date
    )


async def search_stock_news_by_symbol(request: NewsSearchBySymbolRequest) -> dict:
    """Search for stock-related news by ticker symbol or company name."""
    provider = get_provider()
    return await provider.search_stock_news_by_symbol(
        request.symbols, request.page, request.limit, request.from_date, request.to_date
    )


async def search_crypto_news_by_symbol(request: NewsSearchBySymbolRequest) -> dict:
    """Search for cryptocurrency news by coin/token symbol."""
    provider = get_provider()
    return await provider.search_crypto_news_by_symbol(
        request.symbols, request.page, request.limit, request.from_date, request.to_date
    )


async def search_forex_news_by_symbol(request: NewsSearchBySymbolRequest) -> dict:
    """Search for foreign exchange news by currency pair symbol."""
    provider = get_provider()
    return await provider.search_forex_news_by_symbol(
        request.symbols, request.page, request.limit, request.from_date, request.to_date
    )
