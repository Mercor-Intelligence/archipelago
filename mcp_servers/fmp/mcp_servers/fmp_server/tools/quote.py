"""
Stock quote tools using provider pattern.

From FMP API:

stock quote
stock quote short
aftermarket trade
aftermarket quote
stock price change
batch stock quotes
batch stock quotes short
batch aftermarket trades
batch aftermarket quotes
exchange stock quotes
mutual fund quotes
etf quotes
commodity quotes
crypto quotes
forex quotes
index quotes

"""

from mcp_servers.fmp_server.models import (
    AftermarketQuoteRequest,
    AftermarketTradeRequest,
    AllQuotesRequest,
    BatchAftermarketQuotesRequest,
    BatchAftermarketTradesRequest,
    BatchStockQuotesRequest,
    BatchStockQuotesShortRequest,
    ExchangeStockQuotesRequest,
    StockPriceChangeRequest,
    StockQuoteRequest,
    StockQuoteShortRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_stock_quote(request: StockQuoteRequest) -> dict:
    """Access real-time stock quotes."""
    provider = get_provider()
    return await provider.get_stock_quote(request.symbol)


async def get_stock_quote_short(request: StockQuoteShortRequest) -> dict:
    """Get quick snapshots of real-time stock quotes."""
    provider = get_provider()
    return await provider.get_stock_quote_short(request.symbol)


async def get_aftermarket_trade(request: AftermarketTradeRequest) -> dict:
    """Track real-time trading activity after regular market hours."""
    provider = get_provider()
    return await provider.get_aftermarket_trade(request.symbol)


async def get_aftermarket_quote(request: AftermarketQuoteRequest) -> dict:
    """Access real-time aftermarket quotes."""
    provider = get_provider()
    return await provider.get_aftermarket_quote(request.symbol)


async def get_stock_price_change(request: StockPriceChangeRequest) -> dict:
    """Track stock price fluctuations in real-time."""
    provider = get_provider()
    return await provider.get_stock_price_change(request.symbol)


async def get_batch_stock_quotes(request: BatchStockQuotesRequest) -> dict:
    """Retrieve multiple real-time stock quotes in single request."""
    provider = get_provider()
    return await provider.get_batch_stock_quotes(request.symbols, request.limit)


async def get_batch_stock_quotes_short(request: BatchStockQuotesShortRequest) -> dict:
    """Access real-time short-form quotes for multiple stocks."""
    provider = get_provider()
    return await provider.get_batch_stock_quotes_short(request.symbols, request.limit)


async def get_batch_aftermarket_trades(request: BatchAftermarketTradesRequest) -> dict:
    """Retrieve aftermarket trading data for multiple stocks."""
    provider = get_provider()
    return await provider.get_batch_aftermarket_trades(request.symbols)


async def get_batch_aftermarket_quotes(request: BatchAftermarketQuotesRequest) -> dict:
    """Retrieve aftermarket quotes for multiple stocks."""
    provider = get_provider()
    return await provider.get_batch_aftermarket_quotes(request.symbols, request.limit)


async def get_exchange_stock_quotes(request: ExchangeStockQuotesRequest) -> dict:
    """Retrieve real-time stock quotes for all listed stocks on a specific exchange."""
    provider = get_provider()
    return await provider.get_exchange_stock_quotes(request.exchange, request.short, request.limit)


async def get_all_mutualfund_quotes(request: AllQuotesRequest) -> dict:
    """Access real-time quotes for mutual funds."""
    provider = get_provider()
    return await provider.get_all_mutualfund_quotes(request.short, request.limit)


async def get_all_etf_quotes(request: AllQuotesRequest) -> dict:
    """Get real-time price quotes for exchange-traded funds (ETFs)."""
    provider = get_provider()
    return await provider.get_all_etf_quotes(request.short, request.limit)


async def get_all_commodity_quotes(request: AllQuotesRequest) -> dict:
    """Get up-to-the-minute quotes for commodities."""
    provider = get_provider()
    return await provider.get_all_commodity_quotes(request.short, request.limit)


async def get_all_crypto_quotes(request: AllQuotesRequest) -> dict:
    """Access real-time cryptocurrency quotes."""
    provider = get_provider()
    return await provider.get_all_crypto_quotes(request.short, request.limit)


async def get_all_forex_quotes(request: AllQuotesRequest) -> dict:
    """Retrieve real-time quotes for forex currency pairs."""
    provider = get_provider()
    return await provider.get_all_forex_quotes(request.short, request.limit)


async def get_all_index_quotes(request: AllQuotesRequest) -> dict:
    """Track real-time movements of major stock market indexes."""
    provider = get_provider()
    return await provider.get_all_index_quotes(request.short, request.limit)
