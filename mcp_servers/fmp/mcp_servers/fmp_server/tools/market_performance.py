"""
Market performance tools using provider pattern.

From FMP API:

sector performance snapshot
industry performance snapshot
historical sector performance
historical industry performance
sector pe snapshot
industry pe snapshot
historical sector pe
historical industry pe
market movers (gainers/losers/actives)

"""

from mcp_servers.fmp_server.models import (
    HistoricalIndustryPeRequest,
    HistoricalIndustryPerformanceRequest,
    HistoricalSectorPeRequest,
    HistoricalSectorPerformanceRequest,
    IndustryPerformanceSnapshotRequest,
    IndustryPeSnapshotRequest,
    MarketMoversRequest,
    SectorPerformanceSnapshotRequest,
    SectorPeSnapshotRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_sector_performance_snapshot(request: SectorPerformanceSnapshotRequest) -> dict:
    """Get snapshot of sector performance."""
    provider = get_provider()
    return await provider.get_sector_performance_snapshot(
        request.date, request.exchange, request.sector
    )


async def get_industry_performance_snapshot(request: IndustryPerformanceSnapshotRequest) -> dict:
    """Access detailed performance data by industry."""
    provider = get_provider()
    return await provider.get_industry_performance_snapshot(
        request.date, request.exchange, request.industry
    )


async def get_historical_sector_performance(request: HistoricalSectorPerformanceRequest) -> dict:
    """Access historical sector performance data."""
    provider = get_provider()
    return await provider.get_historical_sector_performance(
        request.sector, request.from_date, request.to_date, request.exchange
    )


async def get_historical_industry_performance(
    request: HistoricalIndustryPerformanceRequest,
) -> dict:
    """Access historical performance data for industries."""
    provider = get_provider()
    return await provider.get_historical_industry_performance(
        request.industry, request.from_date, request.to_date, request.exchange
    )


async def get_sector_pe_snapshot(request: SectorPeSnapshotRequest) -> dict:
    """Retrieve price-to-earnings (P/E) ratios for various sectors."""
    provider = get_provider()
    return await provider.get_sector_pe_snapshot(request.date, request.exchange, request.sector)


async def get_industry_pe_snapshot(request: IndustryPeSnapshotRequest) -> dict:
    """View price-to-earnings (P/E) ratios for different industries."""
    provider = get_provider()
    return await provider.get_industry_pe_snapshot(request.date, request.exchange, request.industry)


async def get_historical_sector_pe(request: HistoricalSectorPeRequest) -> dict:
    """Access historical P/E ratios for various sectors."""
    provider = get_provider()
    return await provider.get_historical_sector_pe(
        request.sector, request.exchange, request.from_date, request.to_date
    )


async def get_historical_industry_pe(request: HistoricalIndustryPeRequest) -> dict:
    """Access historical P/E ratios by industry."""
    provider = get_provider()
    return await provider.get_historical_industry_pe(
        request.industry, request.exchange, request.from_date, request.to_date
    )


async def get_biggest_gainers(request: MarketMoversRequest) -> dict:
    """Track stocks with largest price increases."""
    provider = get_provider()
    return await provider.get_biggest_gainers()


async def get_biggest_losers(request: MarketMoversRequest) -> dict:
    """Access data on stocks with largest price drops."""
    provider = get_provider()
    return await provider.get_biggest_losers()


async def get_most_actives(request: MarketMoversRequest) -> dict:
    """View most actively traded stocks."""
    provider = get_provider()
    return await provider.get_most_actives()
