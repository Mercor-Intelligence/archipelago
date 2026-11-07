"""
Commodity tools using provider pattern.

From FMP API:

commodity list

"""

from mcp_servers.fmp_server.models import AssetListRequest
from mcp_servers.fmp_server.providers import get_provider


async def get_commodities_list(request: AssetListRequest) -> dict:
    """Access extensive list of tracked commodities."""
    provider = get_provider()
    return await provider.get_commodities_list(request.limit)
