"""
Stock directory tools using provider pattern.

From FMP API:

all stock symbols
stocks with financials
cik database
recent symbol changes
all etfs
actively trading stocks
companies with transcripts
supported exchanges
all sectors
all industries
all countries

"""

from mcp_servers.fmp_server.models import (
    AssetListRequest,
    EmptyRequest,
    PaginatedCikRequest,
    SymbolChangesRequest,
)
from mcp_servers.fmp_server.providers import get_provider
from mcp_servers.fmp_server.tools._limit import apply_limit

# Safety bound on pages fetched for one stocks-with-financials call.
_MAX_PAGES = 100


async def get_all_stock_symbols(request: AssetListRequest) -> dict:
    """Get comprehensive list of available stock symbols from global exchanges."""
    provider = get_provider()
    return await provider.get_all_stock_symbols(request.limit)


async def get_stocks_with_financials(request: AssetListRequest) -> dict:
    """Get list of companies that have financial statements available."""
    provider = get_provider()
    result = await provider.get_stocks_with_financials()
    # Collect pages until the limit, the reported total, an empty page, or a repeated
    # page (a provider that ignores page). Online responses carry no total.
    page = 0
    while not result.get("error") and isinstance(result.get("data"), list) and page < _MAX_PAGES:
        rows = result["data"]
        if request.limit and len(rows) >= request.limit:
            break
        if result.get("total") is not None and len(rows) >= result["total"]:
            break
        page += 1
        more = await provider.get_stocks_with_financials(page)
        new_rows = more.get("data") if not more.get("error") else None
        if not new_rows or new_rows[0] in rows:
            break
        result = {**result, "data": rows + new_rows}
    result = apply_limit(result, request.limit)
    if "count" in result:
        result["count"] = len(result["data"]) if isinstance(result.get("data"), list) else 0
    return result


async def get_cik_database(request: PaginatedCikRequest) -> dict:
    """Get database of CIK (Central Index Key) numbers for SEC-registered entities."""
    provider = get_provider()
    return await provider.get_cik_database(request.page, request.limit)


async def get_recent_symbol_changes(request: SymbolChangesRequest) -> dict:
    """Get stock symbol changes due to mergers, acquisitions, stock splits, or name changes."""
    provider = get_provider()
    return await provider.get_recent_symbol_changes(request.invalid, request.limit)


async def get_all_etfs(request: AssetListRequest) -> dict:
    """Get complete list of Exchange Traded Funds (ETFs) with ticker symbols and fund names."""
    provider = get_provider()
    return await provider.get_all_etfs(request.limit)


async def get_actively_trading_stocks(request: AssetListRequest) -> dict:
    """Get list of actively trading companies currently being traded on public exchanges."""
    provider = get_provider()
    return await provider.get_actively_trading_stocks(request.limit)


async def get_companies_with_transcripts(request: AssetListRequest) -> dict:
    """Get list of companies that have earnings call transcripts available."""
    provider = get_provider()
    return await provider.get_companies_with_transcripts(request.limit)


async def get_supported_exchanges(request: EmptyRequest) -> dict:
    """Get complete list of all supported stock exchanges worldwide."""
    provider = get_provider()
    return await provider.get_supported_exchanges()


async def get_all_sectors(request: AssetListRequest) -> dict:
    """Get list of all industry sectors."""
    provider = get_provider()
    return apply_limit(await provider.get_all_sectors(), request.limit)


async def get_all_industries(request: AssetListRequest) -> dict:
    """Get comprehensive list of all industries where stocks are available."""
    provider = get_provider()
    return apply_limit(await provider.get_all_industries(), request.limit)


async def get_all_countries(request: AssetListRequest) -> dict:
    """Get list of all countries where stock symbols are available."""
    provider = get_provider()
    return apply_limit(await provider.get_all_countries(), request.limit)
