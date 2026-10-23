"""
Crypto tools using provider pattern.

From FMP API:

cryptocurrency list

"""

from mcp_servers.fmp_server.models import AssetListRequest
from mcp_servers.fmp_server.providers import get_provider


async def get_cryptocurrency_list(request: AssetListRequest) -> dict:
    """Access comprehensive list of all cryptocurrencies traded worldwide."""
    provider = get_provider()
    return await provider.get_cryptocurrency_list(request.limit)
