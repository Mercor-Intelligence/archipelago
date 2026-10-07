"""
Corporate events tools using provider pattern.

From FMP API:

earnings dividends splits
earnings calendar
dividends calendar
ipos calendar
ipos disclosure
ipos prospectus
stock splits
splits calendar

"""

from mcp_servers.fmp_server.models import (
    DateRangeRequest,
    EconomicCalendarRequest,
    EmployeeCountRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_company_dividends(request: EmployeeCountRequest) -> dict:
    """Stay informed about dividend payments."""
    provider = get_provider()
    return await provider.get_company_dividends(request.symbol, request.limit)


async def get_dividends_calendar(request: EconomicCalendarRequest) -> dict:
    """Stay informed on upcoming dividend events."""
    provider = get_provider()
    return await provider.get_dividends_calendar(request.from_date, request.to_date, request.limit)


async def get_company_earnings(request: EmployeeCountRequest) -> dict:
    """Retrieve in-depth earnings information."""
    provider = get_provider()
    return await provider.get_company_earnings(request.symbol, request.limit)


async def get_earnings_calendar(request: EconomicCalendarRequest) -> dict:
    """Stay informed on upcoming and past earnings announcements."""
    provider = get_provider()
    return await provider.get_earnings_calendar(request.from_date, request.to_date, request.limit)


async def get_ipos_calendar(request: EconomicCalendarRequest) -> dict:
    """Access comprehensive list of upcoming initial public offerings (IPOs)."""
    provider = get_provider()
    return await provider.get_ipos_calendar(request.from_date, request.to_date, request.limit)


async def get_ipos_disclosure(request: DateRangeRequest) -> dict:
    """Access comprehensive list of disclosure filings for upcoming IPOs."""
    provider = get_provider()
    return await provider.get_ipos_disclosure(request.from_date, request.to_date)


async def get_ipos_prospectus(request: DateRangeRequest) -> dict:
    """Access comprehensive information on IPO prospectuses."""
    provider = get_provider()
    return await provider.get_ipos_prospectus(request.from_date, request.to_date)


async def get_stock_splits(request: EmployeeCountRequest) -> dict:
    """Access detailed information on stock splits for a specific company."""
    provider = get_provider()
    return await provider.get_stock_splits(request.symbol, request.limit)


async def get_splits_calendar(request: EconomicCalendarRequest) -> dict:
    """Stay informed about upcoming stock splits."""
    provider = get_provider()
    return await provider.get_splits_calendar(request.from_date, request.to_date, request.limit)
