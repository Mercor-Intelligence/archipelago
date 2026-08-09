"""
Economics tools using provider pattern.

From FMP API:

treasury rates
economic indicators
economic calendar
market risk premium

"""

from mcp_servers.fmp_server.models import (
    DateRangeRequest,
    EconomicCalendarRequest,
    EconomicIndicatorRequest,
    EmptyRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_treasury_rates(request: DateRangeRequest) -> dict:
    """Access real-time and historical Treasury rates for all maturities."""
    provider = get_provider()
    return await provider.get_treasury_rates(request.from_date, request.to_date)


async def get_economic_indicators(request: EconomicIndicatorRequest) -> dict:
    """Access real-time and historical economic data for key indicators."""
    provider = get_provider()
    return await provider.get_economic_indicators(request.name, request.from_date, request.to_date)


async def get_economic_calendar(request: EconomicCalendarRequest) -> dict:
    """Stay informed with comprehensive calendar of upcoming economic data releases."""
    provider = get_provider()
    return await provider.get_economic_calendar(request.from_date, request.to_date, request.limit)


async def get_market_risk_premium(request: EmptyRequest) -> dict:
    """Access the market risk premium for specific dates and countries."""
    provider = get_provider()
    return await provider.get_market_risk_premium()
