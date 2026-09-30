"""
Charts and historical price tools using provider pattern.

From FMP API:

historical price light
historical price full
historical price unadjusted
historical price dividend adjusted
intraday 1min
intraday 5min
intraday 15min
intraday 30min
intraday 1hour
intraday 4hour

"""

from datetime import date

from mcp_servers.fmp_server.models import HistoricalPriceRequest, IntradayRequest
from mcp_servers.fmp_server.providers import get_provider


def _check_historical_data_completeness(
    result: dict,
    from_date: str | None,
    to_date: str | None,
) -> dict:
    """Add empty-data error and partial-data notes for historical price results."""
    if result.get("error"):
        return result

    historical = result.get("historical", [])
    if not historical:
        return result

    if from_date and to_date:
        try:
            start = date.fromisoformat(from_date)
            end = date.fromisoformat(to_date)
            calendar_days = (end - start).days
            # ~252 trading days per 365 calendar days
            expected_trading_days = max(1, int(calendar_days * 252 / 365))
            actual_count = len(historical)
            if actual_count < expected_trading_days * 0.8:
                result["_partial_data_note"] = (
                    f"Returned {actual_count} data points for a date range that would "
                    f"typically have ~{expected_trading_days} trading days "
                    f"({from_date} to {to_date}). "
                    "Some dates may be missing due to holidays, data gaps, or "
                    "the symbol not being traded for the full period."
                )
        except (ValueError, TypeError):
            pass

    return result


async def get_historical_price_light(request: HistoricalPriceRequest) -> dict:
    """Access simplified split-adjusted stock chart data (date, open, high, low, close, volume)."""
    provider = get_provider()
    return await provider.get_historical_price_light(
        request.symbol, request.from_date, request.to_date
    )


async def get_historical_price_full(request: HistoricalPriceRequest) -> dict:
    """Access full price and volume data for any stock symbol."""
    provider = get_provider()
    result = await provider.get_historical_price_full(
        request.symbol, request.from_date, request.to_date
    )
    return _check_historical_data_completeness(result, request.from_date, request.to_date)


async def get_historical_price_unadjusted(request: HistoricalPriceRequest) -> dict:
    """Access split-adjusted stock price and volume data (without dividend adjustments)."""
    provider = get_provider()
    return await provider.get_historical_price_unadjusted(
        request.symbol, request.from_date, request.to_date
    )


async def get_historical_price_dividend_adjusted(request: HistoricalPriceRequest) -> dict:
    """Access fully-adjusted historical prices (split-adjusted and dividend-adjusted)."""
    provider = get_provider()
    result = await provider.get_historical_price_dividend_adjusted(
        request.symbol, request.from_date, request.to_date
    )
    return _check_historical_data_completeness(result, request.from_date, request.to_date)


async def get_intraday_1min(request: IntradayRequest) -> dict:
    """Access precise intraday stock price and volume data with 1-minute intervals."""
    provider = get_provider()
    return await provider.get_intraday_1min(
        request.symbol, request.from_date, request.to_date, request.nonadjusted
    )


async def get_intraday_5min(request: IntradayRequest) -> dict:
    """Access stock price and volume data in 5-minute intervals."""
    provider = get_provider()
    return await provider.get_intraday_5min(
        request.symbol, request.from_date, request.to_date, request.nonadjusted
    )


async def get_intraday_15min(request: IntradayRequest) -> dict:
    """Access stock price and volume data in 15-minute intervals."""
    provider = get_provider()
    return await provider.get_intraday_15min(
        request.symbol, request.from_date, request.to_date, request.nonadjusted
    )


async def get_intraday_30min(request: IntradayRequest) -> dict:
    """Access stock price and volume data in 30-minute intervals."""
    provider = get_provider()
    return await provider.get_intraday_30min(
        request.symbol, request.from_date, request.to_date, request.nonadjusted
    )


async def get_intraday_1hour(request: IntradayRequest) -> dict:
    """Track stock price movements over hourly intervals."""
    provider = get_provider()
    return await provider.get_intraday_1hour(
        request.symbol, request.from_date, request.to_date, request.nonadjusted
    )


async def get_intraday_4hour(request: IntradayRequest) -> dict:
    """Analyze stock price movements over extended intraday periods with 4-hour intervals."""
    provider = get_provider()
    return await provider.get_intraday_4hour(
        request.symbol, request.from_date, request.to_date, request.nonadjusted
    )
