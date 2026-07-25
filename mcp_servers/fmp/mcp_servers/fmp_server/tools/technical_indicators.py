"""
Technical indicator tools using provider pattern.

From FMP API:

sma
ema
wma
dema
tema
rsi
standard deviation
williams
adx

"""

from mcp_servers.fmp_server.models import TechnicalIndicatorRequest
from mcp_servers.fmp_server.providers import get_provider


async def get_sma(request: TechnicalIndicatorRequest) -> dict:
    """Get Simple Moving Average (SMA) technical indicator."""
    provider = get_provider()
    return await provider.get_sma(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_ema(request: TechnicalIndicatorRequest) -> dict:
    """Get Exponential Moving Average (EMA) technical indicator."""
    provider = get_provider()
    return await provider.get_ema(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_wma(request: TechnicalIndicatorRequest) -> dict:
    """Get Weighted Moving Average (WMA) technical indicator."""
    provider = get_provider()
    return await provider.get_wma(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_dema(request: TechnicalIndicatorRequest) -> dict:
    """Get Double Exponential Moving Average (DEMA) technical indicator."""
    provider = get_provider()
    return await provider.get_dema(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_tema(request: TechnicalIndicatorRequest) -> dict:
    """Get Triple Exponential Moving Average (TEMA) technical indicator."""
    provider = get_provider()
    return await provider.get_tema(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_rsi(request: TechnicalIndicatorRequest) -> dict:
    """Get Relative Strength Index (RSI) technical indicator."""
    provider = get_provider()
    return await provider.get_rsi(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_standard_deviation(request: TechnicalIndicatorRequest) -> dict:
    """Get Standard Deviation technical indicator."""
    provider = get_provider()
    return await provider.get_standard_deviation(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_williams(request: TechnicalIndicatorRequest) -> dict:
    """Get Williams %R technical indicator."""
    provider = get_provider()
    return await provider.get_williams(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )


async def get_adx(request: TechnicalIndicatorRequest) -> dict:
    """Get Average Directional Index (ADX) technical indicator."""
    provider = get_provider()
    return await provider.get_adx(
        request.symbol, request.periodLength, request.timeframe, request.from_date, request.to_date
    )
