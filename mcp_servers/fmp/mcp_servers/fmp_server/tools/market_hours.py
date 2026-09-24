"""
Market hours tools using provider pattern.

From FMP API:

market hours
holidays
all exchange market hours

"""

from mcp_servers.fmp_server.models import (
    AssetListRequest,
    ExchangeHolidaysRequest,
    ExchangeRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_exchange_market_hours(request: ExchangeRequest) -> dict:
    """Retrieve trading hours for specific stock exchanges."""
    provider = get_provider()
    return await provider.get_exchange_market_hours(request.exchange)


async def get_holidays_by_exchange(request: ExchangeHolidaysRequest) -> dict:
    """View specific holidays for different exchanges."""
    provider = get_provider()
    return await provider.get_holidays_by_exchange(
        request.exchange, request.from_date, request.to_date
    )


async def get_all_exchange_market_hours(request: AssetListRequest) -> dict:
    """Retrieve market hours for all global stock exchanges."""
    provider = get_provider()
    return await provider.get_all_exchange_market_hours()
