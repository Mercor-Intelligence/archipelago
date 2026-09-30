"""
Congressional trading tools using provider pattern.

From FMP API:

house-latest - Get latest House disclosure filings
senate-latest - Get latest Senate disclosure filings
senate-trades - Get Senate trades by symbol
house-trades - Get House trades by symbol

"""

from mcp_servers.fmp_server.models import (
    CongressionalDisclosureRequest,
    CongressionalTradesRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_house_disclosure(request: CongressionalDisclosureRequest) -> dict:
    """Get latest House of Representatives disclosure filings.

    Returns stock trading disclosures from US House members.
    """
    provider = get_provider()
    return await provider.get_house_disclosure(request.page, request.limit)


async def get_senate_disclosure(request: CongressionalDisclosureRequest) -> dict:
    """Get latest Senate disclosure filings.

    Returns stock trading disclosures from US Senate members.
    """
    provider = get_provider()
    return await provider.get_senate_disclosure(request.page, request.limit)


async def get_senate_trades(request: CongressionalTradesRequest) -> dict:
    """Get Senate trading activity for a specific stock symbol.

    Returns trades by US Senate members for the specified symbol.
    """
    provider = get_provider()
    return await provider.get_senate_trades(request.symbol, request.page, request.limit)


async def get_house_trades(request: CongressionalTradesRequest) -> dict:
    """Get House trading activity for a specific stock symbol.

    Returns trades by US House members for the specified symbol.
    """
    provider = get_provider()
    return await provider.get_house_trades(request.symbol, request.page, request.limit)
