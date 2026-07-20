"""
Analyst data tools using provider pattern.

From FMP API:

analyst estimates
ratings
price targets
stock grades

"""

from mcp_servers.fmp_server.models import (
    AnalystPagedRequest,
    CompanySymbolRequest,
    EmployeeCountRequest,
    FinancialStatementRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_analyst_estimates(request: FinancialStatementRequest) -> dict:
    """Get analyst financial estimates (revenue, EPS, EBITDA) for a stock."""
    provider = get_provider()
    return await provider.get_analyst_estimates(request.symbol, request.period, request.limit)


async def get_ratings_snapshot(request: CompanySymbolRequest) -> dict:
    """Get current analyst rating summary for a stock (rating, score, and component scores)."""
    provider = get_provider()
    return await provider.get_ratings_snapshot(request.symbol)


async def get_ratings_historical(request: EmployeeCountRequest) -> dict:
    """Get historical analyst ratings over time for a stock."""
    provider = get_provider()
    return await provider.get_ratings_historical(request.symbol, request.limit)


async def get_price_target_summary(request: CompanySymbolRequest) -> dict:
    """Get analyst price target summary for a stock."""
    provider = get_provider()
    return await provider.get_price_target_summary(request.symbol)


async def get_price_target_consensus(request: CompanySymbolRequest) -> dict:
    """Get consensus price target from all covering analysts."""
    provider = get_provider()
    return await provider.get_price_target_consensus(request.symbol)


async def get_price_target_news(request: EmployeeCountRequest) -> dict:
    """Get recent price target updates for a specific stock."""
    provider = get_provider()
    return await provider.get_price_target_news(request.symbol, 0, request.limit)


async def get_price_target_latest_news(request: AnalystPagedRequest) -> dict:
    """Get latest price target updates across all stocks."""
    provider = get_provider()
    return await provider.get_price_target_latest_news(request.page or 0, request.limit)


async def get_stock_grades(request: CompanySymbolRequest) -> dict:
    """Get individual analyst grades/ratings for a stock."""
    provider = get_provider()
    return await provider.get_stock_grades(request.symbol)


async def get_grades_historical(request: EmployeeCountRequest) -> dict:
    """Get historical analyst grades for a stock over time."""
    provider = get_provider()
    return await provider.get_grades_historical(request.symbol, request.limit)


async def get_grades_consensus(request: CompanySymbolRequest) -> dict:
    """Get consensus analyst grade distribution for a stock."""
    provider = get_provider()
    return await provider.get_grades_consensus(request.symbol)


async def get_grade_news(request: EmployeeCountRequest) -> dict:
    """Get recent grade changes for a specific stock."""
    provider = get_provider()
    return await provider.get_grade_news(request.symbol, 0, request.limit)


async def get_grade_latest_news(request: AnalystPagedRequest) -> dict:
    """Get latest analyst grade changes across all stocks."""
    provider = get_provider()
    return await provider.get_grade_latest_news(request.page or 0, request.limit)
