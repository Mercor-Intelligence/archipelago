"""
SEC filings tools using provider pattern.

From FMP API:

institutional-ownership/latest - Get latest institutional ownership filings
sec-filings-8k - Get latest 8-K SEC filings

"""

from mcp_servers.fmp_server.models import SecFilingsLatestRequest
from mcp_servers.fmp_server.providers import get_provider


async def get_institutional_ownership(request: SecFilingsLatestRequest) -> dict:
    """Get latest institutional ownership filings.

    Returns the most recent 13F institutional ownership filings.
    """
    provider = get_provider()
    return await provider.get_institutional_ownership(request.page, request.limit)


async def get_8k_filings(request: SecFilingsLatestRequest) -> dict:
    """Get latest 8-K SEC filings.

    Returns the most recent 8-K filings which report material events.
    """
    provider = get_provider()
    return await provider.get_8k_filings(request.page, request.limit)
