"""
Index tools using provider pattern.

From FMP API:

index list
sp500 constituents
nasdaq constituents
dowjones constituents
historical sp500 constituents
historical nasdaq constituents
historical dowjones constituents

"""

from mcp_servers.fmp_server.models import AssetListRequest, EmptyRequest
from mcp_servers.fmp_server.providers import get_provider


async def get_index_list(request: AssetListRequest) -> dict:
    """Retrieve comprehensive list of stock market indexes across global exchanges."""
    provider = get_provider()
    return await provider.get_index_list()


async def get_sp500_constituents(request: AssetListRequest) -> dict:
    """Access detailed data on S&P 500 index."""
    provider = get_provider()
    return await provider.get_sp500_constituents(request.limit)


async def get_nasdaq_constituents(request: EmptyRequest) -> dict:
    """Access comprehensive data for Nasdaq index."""
    provider = get_provider()
    return await provider.get_nasdaq_constituents()


async def get_dowjones_constituents(request: EmptyRequest) -> dict:
    """Access data on Dow Jones Industrial Average."""
    provider = get_provider()
    return await provider.get_dowjones_constituents()


async def get_historical_sp500(request: EmptyRequest) -> dict:
    """Retrieve historical data for S&P 500 index."""
    provider = get_provider()
    return await provider.get_historical_sp500()


async def get_historical_nasdaq(request: EmptyRequest) -> dict:
    """Access historical data for Nasdaq index."""
    provider = get_provider()
    return await provider.get_historical_nasdaq()


async def get_historical_dowjones(request: EmptyRequest) -> dict:
    """Access historical data for Dow Jones Industrial Average."""
    provider = get_provider()
    return await provider.get_historical_dowjones()
