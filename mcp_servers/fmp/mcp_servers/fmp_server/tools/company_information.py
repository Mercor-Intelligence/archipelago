"""
Company information tools using provider pattern.

From FMP API:

company profile
company notes
stock peers
delisted companies
employee count
historical employee count
market cap
batch market cap
historical market cap
shares float
all shares float
latest M&A
search M&A
company executives
executive compensation
executive compensation benchmark

"""

from mcp_servers.fmp_server.models import (
    BatchSymbolsRequest,
    CompanyCikRequest,
    CompanyExecutivesRequest,
    CompanySymbolRequest,
    EmployeeCountRequest,
    ExecutiveCompBenchmarkRequest,
    HistoricalDataRequest,
    PaginatedRequest,
    SearchByNameRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_company_profile(request: CompanySymbolRequest) -> dict:
    """Access detailed company profile data."""
    provider = get_provider()
    return await provider.get_company_profile(request.symbol)


async def get_profile_by_cik(request: CompanyCikRequest) -> dict:
    """Retrieve detailed company profile data by CIK."""
    provider = get_provider()
    return await provider.get_profile_by_cik(request.cik)


async def get_company_notes(request: CompanySymbolRequest) -> dict:
    """Retrieve detailed information about company-issued notes."""
    provider = get_provider()
    return await provider.get_company_notes(request.symbol)


async def get_stock_peers(request: CompanySymbolRequest) -> dict:
    """Identify and compare companies within the same sector and market capitalization range."""
    provider = get_provider()
    return await provider.get_stock_peers(request.symbol)


async def get_delisted_companies(request: PaginatedRequest) -> dict:
    """Access delisted companies from US exchanges."""
    provider = get_provider()
    return await provider.get_delisted_companies(request.page, request.limit)


async def get_employee_count(request: EmployeeCountRequest) -> dict:
    """Retrieve workforce information including employee count."""
    provider = get_provider()
    return await provider.get_employee_count(request.symbol, request.limit)


async def get_historical_employee_count(request: EmployeeCountRequest) -> dict:
    """Access historical employee count data."""
    provider = get_provider()
    return await provider.get_historical_employee_count(request.symbol, request.limit)


async def get_market_cap(request: CompanySymbolRequest) -> dict:
    """Retrieve the market capitalization for a specific company."""
    provider = get_provider()
    return await provider.get_market_cap(request.symbol)


async def get_batch_market_cap(request: BatchSymbolsRequest) -> dict:
    """Retrieve market cap for multiple companies in single request."""
    provider = get_provider()
    return await provider.get_batch_market_cap(request.symbols)


async def get_historical_market_cap(request: HistoricalDataRequest) -> dict:
    """Access historical market cap data."""
    provider = get_provider()
    return await provider.get_historical_market_cap(
        request.symbol, request.from_date, request.to_date, request.limit
    )


async def get_shares_float(request: CompanySymbolRequest) -> dict:
    """Get share float and liquidity data."""
    provider = get_provider()
    return await provider.get_shares_float(request.symbol)


async def get_all_shares_float(request: PaginatedRequest) -> dict:
    """Access shares float data for all companies."""
    provider = get_provider()
    return await provider.get_all_shares_float(request.page, request.limit)


async def get_latest_mergers_acquisitions(request: PaginatedRequest) -> dict:
    """Access latest mergers & acquisitions."""
    provider = get_provider()
    return await provider.get_latest_mergers_acquisitions(request.page, request.limit)


async def search_ma(request: SearchByNameRequest) -> dict:
    """Search for M&A deals by company name."""
    provider = get_provider()
    return await provider.search_ma(request.name)


async def get_company_executives(request: CompanyExecutivesRequest) -> dict:
    """Retrieve company executives."""
    provider = get_provider()
    return await provider.get_company_executives(request.symbol, request.active)


async def get_executive_compensation(request: CompanySymbolRequest) -> dict:
    """Retrieve comprehensive executive compensation data."""
    provider = get_provider()
    return await provider.get_executive_compensation(request.symbol)


async def get_executive_comp_benchmark(request: ExecutiveCompBenchmarkRequest) -> dict:
    """Gain access to average executive compensation data across various industries."""
    provider = get_provider()
    return await provider.get_executive_comp_benchmark(request.year)
