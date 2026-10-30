"""
Forex tools using provider pattern.

From FMP API:

forex currency pairs

"""

from mcp_servers.fmp_server.models import AssetListRequest
from mcp_servers.fmp_server.providers import get_provider


async def get_forex_currency_pairs(request: AssetListRequest) -> dict:
    """Access comprehensive list of all currency pairs traded on forex market."""
    provider = get_provider()
    return await provider.get_forex_currency_pairs(request.limit)
