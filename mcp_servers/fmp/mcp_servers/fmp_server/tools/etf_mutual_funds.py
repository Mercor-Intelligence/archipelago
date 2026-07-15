"""
ETF and mutual funds tools using provider pattern.

From FMP API:

etf holdings
etf info
etf country weightings
etf asset exposure
etf sector weightings
fund disclosure holders latest
fund disclosure
fund disclosure dates
search fund disclosure by name

"""

from mcp_servers.fmp_server.models import (
    EmployeeCountRequest,
    EtfSymbolRequest,
    FundDisclosureDatesRequest,
    FundDisclosureRequest,
    FundNameSearchRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_etf_holdings(request: EtfSymbolRequest) -> dict:
    """Get detailed breakdown of assets held within ETFs and mutual funds."""
    provider = get_provider()
    return await provider.get_etf_holdings(request.symbol)


async def get_etf_info(request: EtfSymbolRequest) -> dict:
    """Access comprehensive data on ETFs and mutual funds."""
    provider = get_provider()
    return await provider.get_etf_info(request.symbol)


async def get_etf_country_weightings(request: EtfSymbolRequest) -> dict:
    """Gain insight into how ETFs distribute assets across different countries."""
    provider = get_provider()
    return await provider.get_etf_country_weightings(request.symbol)


async def get_etf_asset_exposure(request: EtfSymbolRequest) -> dict:
    """Discover which ETFs hold specific stocks."""
    provider = get_provider()
    return await provider.get_etf_asset_exposure(request.symbol)


async def get_etf_sector_weightings(request: EtfSymbolRequest) -> dict:
    """Get breakdown of percentage of ETF's assets invested in each sector."""
    provider = get_provider()
    return await provider.get_etf_sector_weightings(request.symbol)


async def get_fund_disclosure_holders_latest(request: EmployeeCountRequest) -> dict:
    """Access latest disclosures from mutual funds and ETFs."""
    provider = get_provider()
    return await provider.get_fund_disclosure_holders_latest(request.symbol, request.limit)


async def get_fund_disclosure(request: FundDisclosureRequest) -> dict:
    """Access comprehensive disclosure data for mutual funds."""
    provider = get_provider()
    return await provider.get_fund_disclosure(
        request.symbol, request.year, request.quarter, request.cik
    )


async def search_fund_disclosure_by_name(request: FundNameSearchRequest) -> dict:
    """Search for mutual fund and ETF disclosures by name."""
    provider = get_provider()
    return await provider.search_fund_disclosure_by_name(request.name)


async def get_fund_disclosure_dates(request: FundDisclosureDatesRequest) -> dict:
    """Retrieve detailed disclosures for mutual funds and ETFs based on filing dates."""
    provider = get_provider()
    return await provider.get_fund_disclosure_dates(request.symbol, request.cik)
