"""
FMP Meta-Tools for LLM Context Optimization.

Consolidates 179 individual tools into 10 domain-based meta-tools.
Each meta-tool supports action="help" for discovery.

Meta-tools:
- fmp_analyst: Analyst ratings, grades, price targets
- fmp_prices: Stock quotes, charts, historical data
- fmp_company: Company profiles, search, directory
- fmp_financials: Financial statements, DCF, earnings
- fmp_market: Market data, performance, economics
- fmp_assets: ETFs, commodities, crypto, forex
- fmp_news: News and press releases
- fmp_government: Congressional trading, SEC filings
- fmp_technical: Technical indicators
- fmp_schema: Tool introspection
"""

import sys
from pathlib import Path
from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from mcp_schema import OutputBaseModel
from pydantic import Field

# Support all execution methods:
# 1. python main.py (direct execution from server dir)
# 2. python -m mcp_servers.fmp_server (module execution)
try:
    from ..providers import get_provider
    from ..utils.schema import flatten_schema
except ImportError:
    # Direct execution - add parent to path for local imports
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from providers import get_provider
    from utils.schema import flatten_schema

# =============================================================================
# Output Models
# =============================================================================


class HelpResponse(OutputBaseModel):
    """Response for action=help requests."""

    tool_name: str
    description: str
    actions: dict[str, dict[str, Any]] = Field(
        description="Actions with required_params, optional_params, description"
    )


# =============================================================================
# Input Models for Meta-Tools
# =============================================================================


class AnalystInput(BaseModel):
    """Input for fmp_analyst meta-tool."""

    action: Literal[
        "help",
        "estimates",
        "ratings_snapshot",
        "ratings_historical",
        "price_target_summary",
        "price_target_consensus",
        "price_target_news",
        "price_target_latest",
        "grades",
        "grades_historical",
        "grades_consensus",
        "grade_news",
        "grade_latest",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Stock ticker symbol (e.g., "AAPL", "MSFT").
REQUIRED for: estimates, ratings_snapshot, ratings_historical, price_target_summary,
price_target_consensus, price_target_news, grades, grades_historical, grades_consensus, grade_news.

Use plain ticker format: "AAPL" not "AAPL US Equity".""",
    )
    period: str | None = Field(
        None,
        description="""Reporting period. Defaults to "annual" if omitted.

For estimates action:
- "annual" - annual estimates
- "quarter" - quarterly estimates

Example: {"action": "estimates", "symbol": "AAPL", "period": "annual"}""",
    )
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")
    page: int | None = Field(
        None, description="Page number (0-indexed). Use with limit for pagination."
    )


class PricesInput(BaseModel):
    """Input for fmp_prices meta-tool."""

    action: Literal[
        "help",
        "quote",
        "quote_short",
        "price_change",
        "aftermarket_quote",
        "aftermarket_trade",
        "batch_quotes",
        "batch_quotes_short",
        "batch_aftermarket_trades",
        "batch_aftermarket_quotes",
        "exchange_quotes",
        "historical_light",
        "historical_full",
        "historical_unadjusted",
        "historical_dividend_adjusted",
        "intraday_1min",
        "intraday_5min",
        "intraday_15min",
        "intraday_30min",
        "intraday_1hour",
        "intraday_4hour",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Single stock ticker symbol. REQUIRED for most actions.

Use for: quote, quote_short, price_change, aftermarket_quote, aftermarket_trade,
historical_light, historical_full, historical_unadjusted, historical_dividend_adjusted,
and ALL intraday_* actions.

VALID FORMATS:
- US stocks: "AAPL", "MSFT", "GOOGL" (plain uppercase ticker)
- US Indices: "^GSPC" (S&P 500), "^DJI" (Dow Jones), "^IXIC" (NASDAQ)
- London Stock Exchange: "ULVR.L", "HSBA.L", "BP.L" (ticker.L)
- Toronto Stock Exchange: "RY.TO", "TD.TO" (ticker.TO)
- Frankfurt/XETRA: "BMW.DE", "SAP.DE" (ticker.DE)
- Paris: "MC.PA", "OR.PA" (ticker.PA)
- Tokyo: "7203.T", "6758.T" (numeric_code.T)
- Hong Kong: "0700.HK", "9988.HK" (number.HK)

INVALID FORMATS:
- "AAPL US Equity" (Bloomberg format)
- "US:AAPL" (some data providers)
- "Apple Inc" (company name)

NOT VALID for treasury yields or economic data - use fmp_market for those.

WRONG: Using "symbols" parameter for single-stock actions
CORRECT: Use "symbol" (singular) for quote, historical_*, intraday_* actions""",
    )
    symbols: str | None = Field(
        None,
        description="""Comma-separated stock symbols. ONLY for batch_* actions.

Use ONLY for: batch_quotes, batch_quotes_short, batch_aftermarket_trades, batch_aftermarket_quotes

Example: "AAPL,MSFT,GOOGL" (no spaces between symbols)

WRONG: Using "symbols" for historical_full or quote actions
CORRECT: Use "symbol" (singular) for non-batch actions""",
    )
    exchange: str | None = Field(
        None,
        description='Exchange code for exchange_quotes action. Values: "NASDAQ", "NYSE", "AMEX"',
    )
    from_date: str | None = Field(
        None, description="Start date (YYYY-MM-DD). Beginning of date range."
    )
    to_date: str | None = Field(
        None, description="End date (YYYY-MM-DD). Defaults to today if omitted."
    )
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")
    short: bool | None = Field(None, description="If true, returns condensed format.")


class CompanyInput(BaseModel):
    """Input for fmp_company meta-tool."""

    action: Literal[
        "help",
        "profile",
        "notes",
        "executives",
        "peers",
        "executive_compensation",
        "compensation_benchmark",
        "share_float",
        "employee_count",
        "grades",
        "revenue_geography",
        "revenue_product",
        "search_symbol",
        "search_name",
        "search_cik",
        "search_cusip",
        "search_isin",
        "screener",
        "list_exchange",
        "list_symbols",
        "list_tradeable",
        "list_etf",
        "list_sp500",
        "list_nasdaq",
        "list_dow",
        "list_index",
        "list_delisted",
        "list_cik",
        "list_statement_symbols",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Stock ticker symbol (e.g., "AAPL", "MSFT").
REQUIRED for: profile, notes, executives, peers, executive_compensation, share_float,
employee_count, grades, revenue_geography, revenue_product.

Use plain ticker format without exchange suffix.""",
    )
    query: str | None = Field(
        None,
        description="""Search text. Format depends on action:
- search_symbol: Ticker symbol or partial (e.g., "AAPL", "AA", "MSF")
- search_name: Company name or partial (e.g., "Apple", "Micro")
- search_cik: Full CIK number (e.g., "320193" for Apple)
- search_cusip: Full 9-character CUSIP (e.g., "037833100")
- search_isin: Full 12-character ISIN (e.g., "US0378331005")

Case-insensitive for symbol/name searches.""",
    )
    exchange: str | None = Field(
        None,
        description='Exchange code. Values: "NASDAQ", "NYSE", "AMEX", "LSE", "TSX", etc. Must be provided for \'search_symbol\' and \'search_name\' actions.',
    )
    sector: str | None = Field(
        None,
        description="""Sector filter for screener action.
VALID VALUES: "Technology", "Healthcare", "Financial Services", "Consumer Cyclical",
"Communication Services", "Industrials", "Consumer Defensive", "Energy",
"Real Estate", "Basic Materials", "Utilities".

Use exact spelling as shown above.""",
    )
    industry: str | None = Field(
        None,
        description='Industry filter for screener (e.g., "Software", "Biotechnology", "Banks").',
    )
    country: str | None = Field(
        None,
        description='ISO country code for screener. Values: "US", "CA", "GB", "DE", "FR".',
    )
    market_cap_min: float | None = Field(
        None,
        description="""Minimum market cap filter in millions USD.
Pass the market cap divided by 1,000,000.
Examples:
- For $1 billion minimum: market_cap_min=1000
- For $50 billion minimum: market_cap_min=50000
- For $100 million minimum: market_cap_min=100""",
    )
    market_cap_max: float | None = Field(
        None,
        description="""Maximum market cap filter in millions USD.
Pass the market cap divided by 1,000,000.
Examples:
- For $10 billion maximum: market_cap_max=10000
- For $1 billion maximum: market_cap_max=1000
- For $500 million maximum: market_cap_max=500""",
    )
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")
    year: int | None = Field(
        None,
        description="Four-digit year (e.g., 2024). Optional. Used for compensation_benchmark action.",
    )


class FinancialsInput(BaseModel):
    """Input for fmp_financials meta-tool."""

    action: Literal[
        "help",
        "income_statement",
        "balance_sheet",
        "cash_flow",
        "income_growth",
        "balance_growth",
        "cash_flow_growth",
        "financial_growth",
        "key_metrics",
        "key_metrics_ttm",
        "ratios",
        "ratios_ttm",
        "financial_score",
        "owner_earnings",
        "enterprise_value",
        "dcf",
        "levered_dcf",
        "dividend_historical",
        "dividend_calendar",
        "splits_historical",
        "splits_calendar",
        "earnings_calendar",
        "earnings_historical",
        "transcript",
        "transcript_dates",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Stock ticker symbol (e.g., "AAPL", "MSFT").
REQUIRED for: income_statement, balance_sheet, cash_flow, all *_growth actions,
key_metrics, key_metrics_ttm, ratios, ratios_ttm, financial_score, owner_earnings,
enterprise_value, dcf, levered_dcf, dividend_historical, splits_historical,
earnings_historical, transcript, transcript_dates.

Use plain ticker format: "AAPL" not "AAPL US Equity".""",
    )
    symbols: str | None = Field(
        None,
        description="""Comma-separated symbols. Currently unused by any action.
Most actions require single "symbol" parameter instead.""",
    )
    period: str | None = Field(
        None,
        description="""Reporting period. Defaults to "annual" if omitted.

For income_statement, balance_sheet, cash_flow, *_growth, key_metrics, ratios:
- "annual" - annual reports
- "quarter" - quarterly reports

Example: {"action": "income_statement", "symbol": "AAPL", "period": "annual"}""",
    )
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")
    year: int | None = Field(
        None,
        description="Four-digit year (e.g., 2024). REQUIRED for transcript action.",
    )
    quarter: int | None = Field(
        None,
        description="Quarter number (1, 2, 3, or 4). REQUIRED for transcript action.",
    )
    from_date: str | None = Field(
        None, description="Start date (YYYY-MM-DD). For calendar actions."
    )
    to_date: str | None = Field(
        None, description="End date (YYYY-MM-DD). Defaults to today if omitted."
    )


class MarketInput(BaseModel):
    """Input for fmp_market meta-tool."""

    action: Literal[
        "help",
        "gainers",
        "losers",
        "most_active",
        "sector_performance",
        "sector_historical",
        "industry_performance",
        "industry_historical",
        "market_hours",
        "exchange_hours",
        "exchange_holidays",
        "index_list",
        "sp500_constituents",
        "nasdaq_constituents",
        "dow_constituents",
        "treasury_rates",
        "economic_indicators",
        "economic_calendar",
        "market_risk_premium",
    ] = Field(..., description="Action to perform")
    sector: str | None = Field(
        None,
        description="""Sector name. REQUIRED for sector_historical. Optional filter for sector_performance.
Values: "Technology", "Healthcare", "Financial Services", "Consumer Cyclical",
"Communication Services", "Industrials", "Consumer Defensive", "Energy",
"Real Estate", "Materials", "Utilities".""",
    )
    industry: str | None = Field(
        None,
        description='Industry name. REQUIRED for industry_historical. Optional filter for industry_performance. Specific industry within sector (e.g., "Software", "Biotechnology").',
    )
    symbol: str | None = Field(
        None,
        description="""Index symbol OR exchange code depending on action:
- For exchange_hours/exchange_holidays: Pass exchange code (e.g., "NASDAQ", "NYSE", "LSE")
- For index-related actions: Pass index symbol (e.g., "^GSPC", "^DJI")""",
    )
    indicator: str | None = Field(
        None,
        description="""Economic indicator name (CASE-SENSITIVE, use exact spelling).

VALID VALUES:
GDP, realGDP, nominalPotentialGDP, gdpPerCapita
CPI, inflationRate
unemploymentRate, totalNonfarmPayroll, initialClaims
federalFunds, 30YearFixedRateMortgageAverage
retailSales, consumerSentiment, durableGoods
industrialProductionTotalIndex, housingStarts

Invalid indicator names will return empty results.""",
    )
    date: str | None = Field(
        None,
        description="Target date (YYYY-MM-DD). REQUIRED for sector/industry performance.",
    )
    from_date: str | None = Field(
        None, description="Start date (YYYY-MM-DD). For historical ranges."
    )
    to_date: str | None = Field(
        None, description="End date (YYYY-MM-DD). Defaults to today if omitted."
    )
    exchange: str | None = Field(
        None,
        description="Exchange code. Optional filter for sector_performance, sector_historical, industry_performance, and industry_historical actions. For exchange_hours/exchange_holidays, pass the exchange code via the symbol parameter instead.",
    )


class AssetsInput(BaseModel):
    """Input for fmp_assets meta-tool."""

    action: Literal[
        "help",
        "etf_list",
        "etf_profile",
        "etf_holdings",
        "etf_sector_weightings",
        "etf_country_weightings",
        "etf_exposure",
        "mutual_fund_search",
        "fund_disclosure",
        "crypto_list",
        "commodity_list",
        "forex_list",
        "all_etf_quotes",
        "all_mutualfund_quotes",
        "all_commodity_quotes",
        "all_crypto_quotes",
        "all_forex_quotes",
        "all_index_quotes",
        "ipo_calendar",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Asset symbol. Format depends on asset type:
- ETF: Plain ticker like "SPY", "QQQ", "VOO"
- Crypto: Paired with USD like "BTCUSD", "ETHUSD", "SOLUSD"
- Forex: Currency pair without separator like "EURUSD", "GBPJPY", "USDJPY"
- Commodity: Symbol+USD like "GCUSD" (gold), "CLUSD" (crude oil), "NGUSD" (natural gas)

Note: Crypto and forex symbols are always paired (e.g., BTCUSD not BTC).""",
    )
    query: str | None = Field(
        None, description="Search text. Matches names, descriptions. Case-insensitive."
    )
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")
    short: bool | None = Field(None, description="If true, returns condensed format.")
    from_date: str | None = Field(
        None, description="Start date (YYYY-MM-DD). Beginning of date range."
    )
    to_date: str | None = Field(
        None, description="End date (YYYY-MM-DD). Defaults to today if omitted."
    )
    year: str | None = Field(
        None,
        description="Four-digit year as string (e.g., '2024'). REQUIRED for fund_disclosure action.",
    )
    quarter: str | None = Field(
        None,
        description="Quarter number as string: '1', '2', '3', or '4'. REQUIRED for fund_disclosure action.",
    )


class NewsInput(BaseModel):
    """Input for fmp_news meta-tool."""

    action: Literal[
        "help",
        "stock_news",
        "forex_news",
        "crypto_news",
        "general_news",
        "press_releases",
        "press_releases_by_symbol",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Single symbol for filtering news. Use for:
- stock_news: Stock ticker (e.g., "AAPL", "MSFT")
- forex_news: Currency pair (e.g., "EURUSD")
- crypto_news: Crypto symbol (e.g., "BTCUSD")

Use 'symbol' (singular) when you want news for ONE asset.
Use 'symbols' (plural, comma-separated) for multiple assets.""",
    )
    symbols: str | None = Field(
        None,
        description="""Comma-separated symbols for batch news filtering.
Example: "AAPL,MSFT,GOOGL" (no spaces).
Alternative to 'symbol' when fetching news for multiple tickers.""",
    )
    page: int | None = Field(
        None, description="Page number (0-indexed, first page is 0). Use with limit for pagination."
    )
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")
    from_date: str | None = Field(
        None, description="Start date (YYYY-MM-DD). Beginning of date range."
    )
    to_date: str | None = Field(
        None, description="End date (YYYY-MM-DD). Defaults to today if omitted."
    )


class GovernmentInput(BaseModel):
    """Input for fmp_government meta-tool (congressional trading & SEC filings)."""

    action: Literal[
        "help",
        "house_disclosure",
        "senate_disclosure",
        "house_trades",
        "senate_trades",
        "institutional_ownership",
        "filings_8k",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Stock ticker symbol (e.g., "AAPL", "MSFT").
REQUIRED for: house_trades, senate_trades.

Use plain ticker format: "AAPL" not "AAPL US Equity".""",
    )
    page: int | None = Field(
        None, description="Page number (0-indexed). Use with limit for pagination."
    )
    limit: int | None = Field(None, description="Max results to return. Typical range: 1-100.")


class TechnicalInput(BaseModel):
    """Input for fmp_technical meta-tool."""

    action: Literal[
        "help",
        "sma",
        "ema",
        "wma",
        "dema",
        "tema",
        "williams",
        "rsi",
        "adx",
        "standard_deviation",
    ] = Field(..., description="Action to perform")
    symbol: str | None = Field(
        None,
        description="""Stock, ETF, crypto, or forex symbol. REQUIRED for all indicator actions.
Examples: "AAPL" (stock), "SPY" (ETF), "BTCUSD" (crypto), "EURUSD" (forex).""",
    )
    period: int = Field(
        14,
        description="""Number of data points/bars for indicator calculation.
Common values: 10, 14, 20 for RSI/Williams; 50, 200, 250 for moving averages.
The time span of each period is determined by the 'interval' parameter.
Range: 1-500.""",
        ge=1,
        le=500,
    )
    interval: str = Field(
        "1day",
        description="""Chart timeframe for each data point.
Valid values: "1min", "5min", "15min", "30min", "1hour", "4hour", "1day".
Example: period=14 with interval="1day" calculates indicator over 14 trading days.""",
    )


class SchemaInput(BaseModel):
    """Input for fmp_schema meta-tool."""

    tool_name: str | None = Field(
        None, description="Tool name to get schema for. If None, lists all tools."
    )


# =============================================================================
# Meta-Tool Functions
# =============================================================================


async def fmp_analyst(request: AnalystInput) -> dict:
    """Analyst ratings, estimates, grades, and price targets. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_analyst",
            description="Analyst ratings, estimates, grades, and price targets.",
            actions={
                "estimates": {
                    "description": "Get analyst financial estimates (revenue, EPS, EBITDA)",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array of estimates with: date, symbol, estimatedRevenueAvg, estimatedRevenueHigh, estimatedRevenueLow, estimatedEpsAvg, numberAnalystsEstimatedRevenue, numberAnalystsEstimatedEps",
                },
                "ratings_snapshot": {
                    "description": "Get current analyst rating summary for a stock",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, rating (Strong Buy/Buy/Hold/Sell/Strong Sell), ratingScore (1-5), ratingRecommendation, and component scores for DCF, ROE, ROA, DE, PE, PB",
                },
                "ratings_historical": {
                    "description": "Get historical ratings over time",
                    "required_params": ["symbol"],
                    "optional_params": ["limit"],
                    "returns": "Array of historical ratings with date and rating scores",
                },
                "price_target_summary": {
                    "description": "Get analyst price target summary",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, lastMonth, lastMonthAvgPriceTarget, lastQuarter, lastQuarterAvgPriceTarget",
                },
                "price_target_consensus": {
                    "description": "Get consensus price target from analysts",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, targetHigh, targetLow, targetConsensus, targetMedian",
                },
                "price_target_news": {
                    "description": "Get recent price target news for a stock",
                    "required_params": ["symbol"],
                    "optional_params": ["limit"],
                    "returns": "Array of price target updates with: symbol, publishedDate, analystName, analystCompany, priceTarget, priceWhenPosted",
                },
                "price_target_latest": {
                    "description": "Get latest price target news across all stocks",
                    "required_params": [],
                    "optional_params": ["page", "limit"],
                    "returns": "Array of latest price target updates across all symbols",
                },
                "grades": {
                    "description": "Get individual analyst grades/ratings for a stock",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array of grades with: symbol, date, gradingCompany, previousGrade, newGrade, gradeAction (upgrade/downgrade/maintain)",
                },
                "grades_historical": {
                    "description": "Get historical stock grades",
                    "required_params": ["symbol"],
                    "optional_params": ["limit"],
                    "returns": "Array of historical grades with date, company, and grade changes",
                },
                "grades_consensus": {
                    "description": "Get consensus analyst grade",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, strongBuy, buy, hold, sell, strongSell, consensus",
                },
                "grade_news": {
                    "description": "Get recent grade changes for a stock",
                    "required_params": ["symbol"],
                    "optional_params": ["limit"],
                    "returns": "Array of recent grade change news for the symbol",
                },
                "grade_latest": {
                    "description": "Get latest grade news across all stocks",
                    "required_params": [],
                    "optional_params": ["page", "limit"],
                    "returns": "Array of latest grade changes across all symbols",
                },
            },
        ).model_dump()

    provider = get_provider()

    if request.action == "estimates":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_analyst_estimates(request.symbol, request.period, request.limit)
    elif request.action == "ratings_snapshot":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_ratings_snapshot(request.symbol)
    elif request.action == "ratings_historical":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_ratings_historical(request.symbol, request.limit)
    elif request.action == "price_target_summary":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_price_target_summary(request.symbol)
    elif request.action == "price_target_consensus":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_price_target_consensus(request.symbol)
    elif request.action == "price_target_news":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        page = request.page or 0
        return await provider.get_price_target_news(request.symbol, page, request.limit)
    elif request.action == "price_target_latest":
        return await provider.get_price_target_latest_news(request.page or 0, request.limit)
    elif request.action == "grades":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_stock_grades(request.symbol)
    elif request.action == "grades_historical":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_grades_historical(request.symbol, request.limit)
    elif request.action == "grades_consensus":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_grades_consensus(request.symbol)
    elif request.action == "grade_news":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_grade_news(request.symbol, request.page or 0, request.limit)
    elif request.action == "grade_latest":
        return await provider.get_grade_latest_news(request.page or 0, request.limit)

    return {"error": f"Unknown action: {request.action}"}


async def fmp_prices(request: PricesInput) -> dict:
    """Stock quotes, historical/intraday prices. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_prices",
            description="Stock quotes, historical prices, and intraday data",
            actions={
                "quote": {
                    "description": "Real-time stock quote. Returns live price during market hours (9:30 AM - 4:00 PM ET). Outside market hours, returns last closing price. Use aftermarket_quote for extended hours trading data.",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, name, price, changesPercentage, change, dayLow, dayHigh, yearHigh, yearLow, marketCap, priceAvg50, priceAvg200, volume, avgVolume, open, previousClose, pe, earningsAnnouncement",
                },
                "quote_short": {
                    "description": "Condensed stock quote with essential price data only",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, price, volume",
                },
                "price_change": {
                    "description": "Price change data over multiple time periods",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, 1D, 5D, 1M, 3M, 6M, ytd, 1Y, 3Y, 5Y, 10Y, max percentage changes",
                },
                "aftermarket_quote": {
                    "description": "After-hours/pre-market quote for extended trading sessions (4:00 PM - 8:00 PM ET and 4:00 AM - 9:30 AM ET)",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, price, and aftermarket session data",
                },
                "aftermarket_trade": {
                    "description": "After-hours trade data including last trade details",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, price, size, timestamp of aftermarket trades",
                },
                "batch_quotes": {
                    "description": "Multiple stock quotes in a single request",
                    "required_params": ["symbols"],
                    "optional_params": ["limit"],
                    "returns": "Array of full quote objects for each requested symbol",
                },
                "batch_quotes_short": {
                    "description": "Multiple condensed quotes in a single request",
                    "required_params": ["symbols"],
                    "optional_params": ["limit"],
                    "returns": "Array of short quote objects (symbol, price, volume) for each symbol",
                },
                "batch_aftermarket_trades": {
                    "description": "Multiple after-hours trades in a single request",
                    "required_params": ["symbols"],
                    "optional_params": [],
                    "returns": "Array of aftermarket trade data for each symbol",
                },
                "batch_aftermarket_quotes": {
                    "description": "Multiple after-hours quotes in a single request",
                    "required_params": ["symbols"],
                    "optional_params": ["limit"],
                    "returns": "Array of aftermarket quote data for each symbol",
                },
                "exchange_quotes": {
                    "description": "All quotes from a specific exchange",
                    "required_params": ["exchange"],
                    "optional_params": ["short", "limit"],
                    "returns": "Array of quote objects for all stocks on the specified exchange",
                },
                "historical_light": {
                    "description": "Historical daily prices in compact format",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, close, change, changePercent",
                },
                "historical_full": {
                    "description": "Historical daily prices with full OHLCV data",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, open, high, low, close, adjClose, volume, unadjustedVolume, change, changePercent, vwap",
                },
                "historical_unadjusted": {
                    "description": "Unadjusted historical prices (not adjusted for splits/dividends)",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, open, high, low, close, volume (unadjusted values)",
                },
                "historical_dividend_adjusted": {
                    "description": "Historical prices adjusted for dividends only",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, open, high, low, close, volume (dividend-adjusted)",
                },
                "intraday_1min": {
                    "description": "1-minute intraday OHLCV data",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date (timestamp), open, high, low, close, volume per minute",
                },
                "intraday_5min": {
                    "description": "5-minute intraday OHLCV data",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date (timestamp), open, high, low, close, volume per 5 minutes",
                },
                "intraday_15min": {
                    "description": "15-minute intraday OHLCV data",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date (timestamp), open, high, low, close, volume per 15 minutes",
                },
                "intraday_30min": {
                    "description": "30-minute intraday OHLCV data",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date (timestamp), open, high, low, close, volume per 30 minutes",
                },
                "intraday_1hour": {
                    "description": "1-hour intraday OHLCV data",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date (timestamp), open, high, low, close, volume per hour",
                },
                "intraday_4hour": {
                    "description": "4-hour intraday OHLCV data",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date (timestamp), open, high, low, close, volume per 4 hours",
                },
            },
        ).model_dump()

    provider = get_provider()

    # Quote actions
    if request.action == "quote":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_stock_quote(request.symbol)
    elif request.action == "quote_short":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_stock_quote_short(request.symbol)
    elif request.action == "price_change":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_stock_price_change(request.symbol)
    elif request.action == "aftermarket_quote":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_aftermarket_quote(request.symbol)
    elif request.action == "aftermarket_trade":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_aftermarket_trade(request.symbol)
    elif request.action == "batch_quotes":
        if not request.symbols:
            return {"error": "Missing required parameter: symbols"}
        return await provider.get_batch_stock_quotes(request.symbols, request.limit)
    elif request.action == "batch_quotes_short":
        if not request.symbols:
            return {"error": "Missing required parameter: symbols"}
        return await provider.get_batch_stock_quotes_short(request.symbols, request.limit)
    elif request.action == "batch_aftermarket_trades":
        if not request.symbols:
            return {"error": "Missing required parameter: symbols"}
        return await provider.get_batch_aftermarket_trades(request.symbols)
    elif request.action == "batch_aftermarket_quotes":
        if not request.symbols:
            return {"error": "Missing required parameter: symbols"}
        return await provider.get_batch_aftermarket_quotes(request.symbols, request.limit)
    elif request.action == "exchange_quotes":
        if not request.exchange:
            return {"error": "Missing required parameter: exchange"}
        return await provider.get_exchange_stock_quotes(
            request.exchange, request.short, request.limit
        )
    # Historical/charts actions
    elif request.action == "historical_light":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_historical_price_light(
            request.symbol, request.from_date, request.to_date
        )
    elif request.action == "historical_full":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_historical_price_full(
            request.symbol, request.from_date, request.to_date
        )
    elif request.action == "historical_unadjusted":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_historical_price_unadjusted(
            request.symbol, request.from_date, request.to_date
        )
    elif request.action == "historical_dividend_adjusted":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_historical_price_dividend_adjusted(
            request.symbol, request.from_date, request.to_date
        )
    # Intraday actions
    elif request.action == "intraday_1min":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_intraday_1min(request.symbol, request.from_date, request.to_date)
    elif request.action == "intraday_5min":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_intraday_5min(request.symbol, request.from_date, request.to_date)
    elif request.action == "intraday_15min":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_intraday_15min(request.symbol, request.from_date, request.to_date)
    elif request.action == "intraday_30min":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_intraday_30min(request.symbol, request.from_date, request.to_date)
    elif request.action == "intraday_1hour":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_intraday_1hour(request.symbol, request.from_date, request.to_date)
    elif request.action == "intraday_4hour":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_intraday_4hour(request.symbol, request.from_date, request.to_date)

    return {"error": f"Unknown action: {request.action}"}


async def fmp_company(request: CompanyInput) -> dict:
    """Company profiles, search, and stock directory. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_company",
            description="Company profiles, search, and stock directory",
            actions={
                "profile": {
                    "description": "Full company profile: name, sector, industry, description, CEO, employees, headquarters, website, market cap, beta, P/E, DCF valuation",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, companyName, currency, cik, isin, cusip, exchange, industry, website, description, CEO, sector, country, fullTimeEmployees, phone, address, city, state, zip, dcfDiff, dcf, image, ipoDate, defaultImage, beta, volAvg, mktCap, lastDiv, range, changes, price, isActivelyTrading, isEtf, isFund",
                },
                "peers": {
                    "description": "List of similar companies in the same sector/industry",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array of ticker symbols for peer companies",
                },
                "executive_compensation": {
                    "description": "Executive compensation data including salary, bonus, and stock awards",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with: cik, symbol, companyName, filingDate, acceptedDate, nameAndPosition, year, salary, bonus, stockAward, incentivePlan, allOtherComp, total",
                },
                "compensation_benchmark": {
                    "description": "Executive compensation benchmarks across companies by year",
                    "required_params": [],
                    "optional_params": ["year"],
                    "returns": "Array of compensation benchmark data for the specified year",
                },
                "share_float": {
                    "description": "Share float data: shares available for public trading",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, date, freeFloat, floatShares, outstandingShares, source",
                },
                "employee_count": {
                    "description": "Historical employee count data",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with: symbol, cik, acceptanceTime, periodOfReport, companyName, formType, filingDate, employeeCount, source",
                },
                "grades": {
                    "description": "Stock grades from analysts (same as fmp_analyst grades)",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array of grades with: symbol, date, gradingCompany, previousGrade, newGrade",
                },
                "revenue_geography": {
                    "description": "Revenue breakdown by geographic region",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with geographic segment revenue data",
                },
                "revenue_product": {
                    "description": "Revenue breakdown by product/service segment",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with product segment revenue data",
                },
                "notes": {
                    "description": "SEC filing footnotes and disclosures: risk factors, accounting policies, debt details, legal proceedings",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with SEC filing notes and disclosures",
                },
                "executives": {
                    "description": "List of company executives with titles and compensation",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with: title, name, pay, currencyPay, gender, yearBorn, titleSince",
                },
                "search_symbol": {
                    "description": "Search companies by ticker symbol (partial match supported)",
                    "required_params": ["query", "exchange"],
                    "optional_params": ["limit"],
                    "returns": "Array with: symbol, name, currency, stockExchange, exchangeShortName",
                },
                "search_name": {
                    "description": "Search companies by name (partial match supported)",
                    "required_params": ["query", "exchange"],
                    "optional_params": ["limit"],
                    "returns": "Array with: symbol, name, currency, stockExchange, exchangeShortName",
                },
                "search_cik": {
                    "description": "Look up company by CIK number (SEC identifier)",
                    "required_params": ["query"],
                    "optional_params": [],
                    "returns": "Object with company information matching the CIK",
                },
                "search_cusip": {
                    "description": "Look up company by CUSIP (9-character identifier)",
                    "required_params": ["query"],
                    "optional_params": [],
                    "returns": "Object with company information matching the CUSIP",
                },
                "search_isin": {
                    "description": "Look up company by ISIN (12-character international identifier)",
                    "required_params": ["query"],
                    "optional_params": [],
                    "returns": "Object with company information matching the ISIN",
                },
                "screener": {
                    "description": "Screen stocks by multiple criteria. All three required params must be provided.",
                    "required_params": ["exchange", "sector", "industry"],
                    "optional_params": ["country", "market_cap_min", "market_cap_max", "limit"],
                    "returns": "Array of stocks matching the filter criteria",
                    "example": {
                        "action": "screener",
                        "exchange": "NASDAQ",
                        "sector": "Technology",
                        "industry": "Software - Application",
                        "country": "US",
                        "market_cap_min": 10000,
                        "limit": 25,
                    },
                },
                "list_exchange": {
                    "description": "List all stocks on a specific exchange",
                    "required_params": ["exchange"],
                    "optional_params": [],
                    "returns": "Array of stocks listed on the specified exchange",
                },
                "list_symbols": {
                    "description": "List all available stock symbols in the database",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of all stock symbols with basic info",
                },
                "list_tradeable": {
                    "description": "List all currently tradeable symbols (excludes suspended/delisted)",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of actively trading stock symbols",
                },
                "list_etf": {
                    "description": "List all available ETF symbols",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of ETF symbols with basic info",
                },
                "list_sp500": {
                    "description": "List current S&P 500 index constituents",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array with: symbol, name, sector, subSector, headQuarter, dateFirstAdded, cik, founded",
                },
                "list_nasdaq": {
                    "description": "List current NASDAQ 100 index constituents",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of NASDAQ 100 constituent stocks",
                },
                "list_dow": {
                    "description": "List current Dow Jones Industrial Average constituents",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of Dow Jones 30 constituent stocks",
                },
                "list_index": {
                    "description": "List all available market indexes",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of index symbols and names",
                },
                "list_delisted": {
                    "description": "List companies that have been delisted",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of delisted companies with delisting date and reason",
                },
                "list_cik": {
                    "description": "List all CIK numbers mapped to company symbols",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array with: cik, symbol mapping",
                },
                "list_statement_symbols": {
                    "description": "List symbols that have financial statements available",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of symbols with available financial statements",
                },
            },
        ).model_dump()

    provider = get_provider()

    # Company information actions
    if request.action == "profile":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_company_profile(request.symbol)
    elif request.action == "notes":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_company_notes(request.symbol)
    elif request.action == "executives":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_company_executives(request.symbol)
    elif request.action == "peers":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_stock_peers(request.symbol)
    elif request.action == "executive_compensation":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_executive_compensation(request.symbol)
    elif request.action == "compensation_benchmark":
        return await provider.get_executive_comp_benchmark(request.year)
    elif request.action == "share_float":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_shares_float(request.symbol)
    elif request.action == "employee_count":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_employee_count(request.symbol)
    elif request.action == "grades":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_stock_grades(request.symbol)
    elif request.action == "revenue_geography":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_revenue_by_geography(request.symbol)
    elif request.action == "revenue_product":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_revenue_by_product(request.symbol)
    # Search actions
    elif request.action == "search_symbol":
        if not request.query:
            return {"error": "Missing required parameter: query"}
        if not request.exchange:
            return {"error": "Missing required parameter: exchange"}
        return await provider.search_by_symbol(request.query, request.limit, request.exchange)
    elif request.action == "search_name":
        if not request.query:
            return {"error": "Missing required parameter: query"}
        if not request.exchange:
            return {"error": "Missing required parameter: exchange"}
        return await provider.search_by_company_name(request.query, request.limit, request.exchange)
    elif request.action == "search_cik":
        if not request.query:
            return {"error": "Missing required parameter: query"}
        return await provider.search_by_cik(request.query)
    elif request.action == "search_cusip":
        if not request.query:
            return {"error": "Missing required parameter: query"}
        return await provider.search_by_cusip(request.query)
    elif request.action == "search_isin":
        if not request.query:
            return {"error": "Missing required parameter: query"}
        return await provider.search_by_isin(request.query)
    elif request.action == "screener":
        if not request.exchange:
            return {"error": "Missing required parameter: exchange"}
        if not request.sector:
            return {"error": "Missing required parameter: sector"}
        if not request.industry:
            return {"error": "Missing required parameter: industry"}
        return await provider.screen_stocks(
            exchange=request.exchange,
            sector=request.sector,
            industry=request.industry,
            country=request.country,
            market_cap_more_than=int(request.market_cap_min) if request.market_cap_min else None,
            market_cap_lower_than=int(request.market_cap_max) if request.market_cap_max else None,
            limit=request.limit,
        )
    elif request.action == "list_exchange":
        if not request.exchange:
            return {"error": "Missing required parameter: exchange"}
        return await provider.find_exchange_listings(request.exchange)
    # Stock directory actions
    elif request.action == "list_symbols":
        return await provider.get_all_stock_symbols()
    elif request.action == "list_tradeable":
        return await provider.get_actively_trading_stocks()
    elif request.action == "list_etf":
        return await provider.get_all_etfs()
    elif request.action == "list_sp500":
        return await provider.get_sp500_constituents()
    elif request.action == "list_nasdaq":
        return await provider.get_nasdaq_constituents()
    elif request.action == "list_dow":
        return await provider.get_dowjones_constituents()
    elif request.action == "list_index":
        return await provider.get_index_list()
    elif request.action == "list_delisted":
        return await provider.get_delisted_companies()
    elif request.action == "list_cik":
        return await provider.get_cik_database()
    elif request.action == "list_statement_symbols":
        return await provider.get_stocks_with_financials()

    return {"error": f"Unknown action: {request.action}"}


async def fmp_financials(request: FinancialsInput) -> dict:
    """Financial statements, valuations, earnings, and dividends. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_financials",
            description="Financial statements, valuations, earnings, and dividends",
            actions={
                "income_statement": {
                    "description": "Income statement: revenue, expenses, net income",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with: date, symbol, reportedCurrency, revenue, costOfRevenue, grossProfit, operatingExpenses, operatingIncome, netIncome, eps, epsDiluted",
                },
                "balance_sheet": {
                    "description": "Balance sheet: assets, liabilities, equity",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with: date, symbol, totalAssets, totalCurrentAssets, totalLiabilities, totalEquity, cashAndCashEquivalents, totalDebt, netDebt",
                },
                "cash_flow": {
                    "description": "Cash flow statement: operating, investing, financing activities",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with: date, symbol, operatingCashFlow, capitalExpenditure, freeCashFlow, dividendsPaid, netCashFromFinancing, netCashFromInvesting",
                },
                "income_growth": {
                    "description": "Year-over-year income statement growth rates",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with growth percentages for revenue, netIncome, eps, etc.",
                },
                "balance_growth": {
                    "description": "Year-over-year balance sheet growth rates",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with growth percentages for assets, liabilities, equity, etc.",
                },
                "cash_flow_growth": {
                    "description": "Year-over-year cash flow growth rates",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with growth percentages for operating cash flow, free cash flow, etc.",
                },
                "financial_growth": {
                    "description": "Overall financial growth metrics",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with comprehensive growth metrics",
                },
                "key_metrics": {
                    "description": "Key financial metrics: ROE, ROA, debt ratios, margins",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with: revenuePerShare, netIncomePerShare, operatingCashFlowPerShare, freeCashFlowPerShare, peRatio, priceToSalesRatio, pbRatio, debtToEquity, currentRatio, roe, roa",
                },
                "key_metrics_ttm": {
                    "description": "Trailing twelve months (TTM) key metrics",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with TTM values for key financial metrics",
                },
                "ratios": {
                    "description": "Comprehensive financial ratios",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with profitability, liquidity, solvency, and efficiency ratios",
                },
                "ratios_ttm": {
                    "description": "Trailing twelve months (TTM) financial ratios",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with TTM financial ratios",
                },
                "financial_score": {
                    "description": "Financial health score (Altman Z-score, Piotroski score)",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: altmanZScore, piotroskiScore, workingCapital, totalAssets, retainedEarnings, ebit, marketCap, totalLiabilities, revenue",
                },
                "owner_earnings": {
                    "description": "Owner earnings (Warren Buffett's preferred metric)",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: date, symbol, ownerEarnings, growthCapex, maintenanceCapex",
                },
                "enterprise_value": {
                    "description": "Enterprise value calculation",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "limit"],
                    "returns": "Array with: symbol, date, stockPrice, numberOfShares, marketCapitalization, minusCashAndCashEquivalents, addTotalDebt, enterpriseValue",
                },
                "dcf": {
                    "description": "Discounted cash flow (DCF) valuation",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, date, dcf, stockPrice (compare dcf to stockPrice for under/overvaluation)",
                },
                "levered_dcf": {
                    "description": "Levered DCF valuation (accounts for debt)",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with levered DCF valuation data",
                },
                "dividend_historical": {
                    "description": "Historical dividend payments for a company",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with: date, label, adjDividend, dividend, recordDate, paymentDate, declarationDate",
                },
                "dividend_calendar": {
                    "description": "Upcoming ex-dividend dates across all stocks",
                    "required_params": [],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array of upcoming dividend events with symbol, exDate, paymentDate, amount",
                },
                "splits_historical": {
                    "description": "Historical stock splits for a company",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with: date, label, numerator, denominator",
                },
                "splits_calendar": {
                    "description": "Upcoming stock splits across all stocks",
                    "required_params": [],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array of upcoming split events with symbol, date, ratio",
                },
                "earnings_calendar": {
                    "description": "Upcoming earnings announcement dates across all stocks",
                    "required_params": [],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, symbol, eps, epsEstimated, time (before/after market), revenue, revenueEstimated",
                },
                "earnings_historical": {
                    "description": "Historical earnings results for a company",
                    "required_params": ["symbol"],
                    "optional_params": ["limit"],
                    "returns": "Array with: date, symbol, eps, epsEstimated, revenue, revenueEstimated",
                },
                "transcript": {
                    "description": "Full earnings call transcript text",
                    "required_params": ["symbol", "year", "quarter"],
                    "optional_params": [],
                    "returns": "Object with: symbol, quarter, year, date, content (full transcript text)",
                },
                "transcript_dates": {
                    "description": "Available transcript dates for a company",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array of available transcript dates with year and quarter",
                },
            },
        ).model_dump()

    provider = get_provider()

    # Financial statements
    if request.action == "income_statement":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_income_statement(request.symbol, request.period, request.limit)
    elif request.action == "balance_sheet":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_balance_sheet(request.symbol, request.period, request.limit)
    elif request.action == "cash_flow":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_cash_flow_statement(request.symbol, request.period, request.limit)
    elif request.action == "income_growth":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_income_growth(request.symbol, request.period, request.limit)
    elif request.action == "balance_growth":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_balance_sheet_growth(
            request.symbol, request.period, request.limit
        )
    elif request.action == "cash_flow_growth":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_cash_flow_growth(request.symbol, request.period, request.limit)
    elif request.action == "financial_growth":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_financial_growth(request.symbol, request.period, request.limit)
    elif request.action == "key_metrics":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_key_metrics(request.symbol, request.period, request.limit)
    elif request.action == "key_metrics_ttm":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_key_metrics_ttm(request.symbol)
    elif request.action == "ratios":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_financial_ratios(request.symbol, request.period, request.limit)
    elif request.action == "ratios_ttm":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_ratios_ttm(request.symbol)
    elif request.action == "financial_score":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_financial_scores(request.symbol)
    elif request.action == "owner_earnings":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_owner_earnings(request.symbol)
    elif request.action == "enterprise_value":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_enterprise_values(request.symbol, request.period, request.limit)
    # DCF Valuation
    elif request.action == "dcf":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_dcf_valuation(request.symbol)
    elif request.action == "levered_dcf":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_levered_dcf_valuation(request.symbol)
    # Dividends and splits
    elif request.action == "dividend_historical":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_company_dividends(request.symbol)
    elif request.action == "dividend_calendar":
        return await provider.get_dividends_calendar(request.from_date, request.to_date)
    elif request.action == "splits_historical":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_stock_splits(request.symbol)
    elif request.action == "splits_calendar":
        return await provider.get_splits_calendar(request.from_date, request.to_date)
    # Earnings
    elif request.action == "earnings_calendar":
        return await provider.get_earnings_calendar(request.from_date, request.to_date)
    elif request.action == "earnings_historical":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_company_earnings(request.symbol, request.limit)
    # Transcripts
    elif request.action == "transcript":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        if not request.year:
            return {"error": "Missing required parameter: year"}
        if not request.quarter:
            return {"error": "Missing required parameter: quarter"}
        return await provider.get_earning_call_transcript(
            request.symbol, request.year, request.quarter
        )
    elif request.action == "transcript_dates":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_transcript_dates_by_symbol(request.symbol)

    return {"error": f"Unknown action: {request.action}"}


async def fmp_market(request: MarketInput) -> dict:
    """Market data: sectors, industries, indexes, economic indicators, treasury rates. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_market",
            description="Market performance, indexes, and economic data",
            actions={
                "gainers": {
                    "description": "Top gaining stocks today by percentage change",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of stocks with: symbol, name, change, price, changesPercentage",
                },
                "losers": {
                    "description": "Top losing stocks today by percentage change",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of stocks with: symbol, name, change, price, changesPercentage",
                },
                "most_active": {
                    "description": "Most actively traded stocks today by volume",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of stocks with: symbol, name, price, volume, changesPercentage",
                },
                "sector_performance": {
                    "description": "Sector performance on a specific date",
                    "required_params": ["date"],
                    "optional_params": ["exchange", "sector"],
                    "returns": "Array of sector performance data with percentage changes",
                },
                "sector_historical": {
                    "description": "Historical performance for a specific sector over time",
                    "required_params": ["sector"],
                    "optional_params": ["from_date", "to_date", "exchange"],
                    "returns": "Array of historical sector performance data",
                },
                "industry_performance": {
                    "description": "Industry performance on a specific date",
                    "required_params": ["date"],
                    "optional_params": ["exchange", "industry"],
                    "returns": "Array of industry performance data with percentage changes",
                },
                "industry_historical": {
                    "description": "Historical performance for a specific industry over time",
                    "required_params": ["industry"],
                    "optional_params": ["from_date", "to_date", "exchange"],
                    "returns": "Array of historical industry performance data",
                },
                "market_hours": {
                    "description": "Market hours for all major exchanges worldwide",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of exchange hours with: stockExchangeName, stockMarketHours, timezone, isMarketOpen",
                },
                "exchange_hours": {
                    "description": "Get market hours for a specific exchange. Pass exchange code (NASDAQ, NYSE, LSE) in 'symbol' parameter.",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with exchange name, open/close times, timezone, and isOpen status",
                },
                "exchange_holidays": {
                    "description": "Get holidays for a specific exchange. Pass exchange code (NASDAQ, NYSE, LSE) in 'symbol' parameter.",
                    "required_params": ["symbol"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array of holidays with date, name, and exchange",
                },
                "index_list": {
                    "description": "List of all available market indexes",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of index symbols and names",
                },
                "sp500_constituents": {
                    "description": "Current S&P 500 index constituents",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array with: symbol, name, sector, subSector, headQuarter, dateFirstAdded",
                },
                "nasdaq_constituents": {
                    "description": "Current NASDAQ 100 index constituents",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of NASDAQ 100 constituent stocks",
                },
                "dow_constituents": {
                    "description": "Current Dow Jones Industrial Average constituents",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of Dow Jones 30 constituent stocks",
                },
                "treasury_rates": {
                    "description": "US Treasury rates (yields) for various maturities",
                    "required_params": [],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, month1, month2, month3, month6, year1, year2, year3, year5, year7, year10, year20, year30",
                },
                "economic_indicators": {
                    "description": "Economic indicator data (GDP, CPI, unemployment, etc.)",
                    "required_params": ["indicator"],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, value for the specified indicator",
                },
                "economic_calendar": {
                    "description": "Economic events calendar (Fed meetings, jobs reports, etc.)",
                    "required_params": [],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array of economic events with: event, date, country, actual, previous, estimate, impact",
                },
                "market_risk_premium": {
                    "description": "Current market risk premium for equity valuation",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Object with: country, marketRiskPremium, totalEquityRiskPremium, countryRiskPremium",
                },
            },
        ).model_dump()

    provider = get_provider()

    # Market performance
    if request.action == "gainers":
        return await provider.get_biggest_gainers()
    elif request.action == "losers":
        return await provider.get_biggest_losers()
    elif request.action == "most_active":
        return await provider.get_most_actives()
    elif request.action == "sector_performance":
        if not request.date:
            return {"error": "Missing required parameter: date"}
        return await provider.get_sector_performance_snapshot(
            request.date, request.exchange, request.sector
        )
    elif request.action == "sector_historical":
        if not request.sector:
            return {"error": "Missing required parameter: sector"}
        return await provider.get_historical_sector_performance(
            request.sector, request.from_date, request.to_date, request.exchange
        )
    elif request.action == "industry_performance":
        if not request.date:
            return {"error": "Missing required parameter: date"}
        return await provider.get_industry_performance_snapshot(
            request.date, request.exchange, request.industry
        )
    elif request.action == "industry_historical":
        if not request.industry:
            return {"error": "Missing required parameter: industry"}
        return await provider.get_historical_industry_performance(
            request.industry, request.from_date, request.to_date, request.exchange
        )
    # Market hours
    elif request.action == "market_hours":
        return await provider.get_all_exchange_market_hours()
    elif request.action == "exchange_hours":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_exchange_market_hours(request.symbol)
    elif request.action == "exchange_holidays":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_holidays_by_exchange(
            request.symbol, request.from_date, request.to_date
        )
    # Indexes
    elif request.action == "index_list":
        return await provider.get_index_list()
    elif request.action == "sp500_constituents":
        return await provider.get_sp500_constituents()
    elif request.action == "nasdaq_constituents":
        return await provider.get_nasdaq_constituents()
    elif request.action == "dow_constituents":
        return await provider.get_dowjones_constituents()
    # Economics
    elif request.action == "treasury_rates":
        return await provider.get_treasury_rates(request.from_date, request.to_date)
    elif request.action == "economic_indicators":
        if not request.indicator:
            return {"error": "Missing required parameter: indicator"}
        return await provider.get_economic_indicators(
            request.indicator, request.from_date, request.to_date
        )
    elif request.action == "economic_calendar":
        return await provider.get_economic_calendar(request.from_date, request.to_date)
    elif request.action == "market_risk_premium":
        return await provider.get_market_risk_premium()

    return {"error": f"Unknown action: {request.action}"}


async def fmp_assets(request: AssetsInput) -> dict:
    """Non-stock assets: ETFs, mutual funds, commodities, crypto, forex. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_assets",
            description="ETFs, mutual funds, commodities, crypto, and forex",
            actions={
                "etf_list": {
                    "description": "List all available ETFs",
                    "required_params": [],
                    "optional_params": ["limit"],
                    "returns": "Array of ETF symbols with basic info",
                },
                "etf_profile": {
                    "description": "ETF profile: expense ratio, AUM, inception date, holdings count",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with: symbol, name, expenseRatio, aum, avgVolume, nav, description, inceptionDate, holdingsCount",
                },
                "etf_holdings": {
                    "description": "Individual holdings within an ETF",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array of holdings with: asset, name, shares, weightPercentage, marketValue",
                },
                "etf_sector_weightings": {
                    "description": "ETF allocation by sector",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with: sector, weightPercentage",
                },
                "etf_country_weightings": {
                    "description": "ETF geographic allocation by country",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Array with: country, weightPercentage",
                },
                "etf_exposure": {
                    "description": "ETF exposure analysis by asset class",
                    "required_params": ["symbol"],
                    "optional_params": [],
                    "returns": "Object with ETF asset exposure breakdown",
                },
                "mutual_fund_search": {
                    "description": "Search for mutual funds by name",
                    "required_params": ["query"],
                    "optional_params": ["limit"],
                    "returns": "Array of matching mutual funds",
                },
                "fund_disclosure": {
                    "description": "13F fund holdings disclosure data from SEC filings",
                    "required_params": ["symbol", "year", "quarter"],
                    "optional_params": [],
                    "returns": "Array of 13F holdings with: cik, filingDate, ticker, companyName, shares, value",
                    "example": {
                        "action": "fund_disclosure",
                        "symbol": "SPY",
                        "year": "2024",
                        "quarter": "3",
                    },
                },
                "commodity_list": {
                    "description": "List of available commodity symbols",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of commodity symbols (e.g., GCUSD for gold, CLUSD for crude oil)",
                },
                "crypto_list": {
                    "description": "List of available cryptocurrency symbols",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of crypto symbols (e.g., BTCUSD, ETHUSD)",
                },
                "forex_list": {
                    "description": "List of available forex currency pairs",
                    "required_params": [],
                    "optional_params": [],
                    "returns": "Array of forex pairs (e.g., EURUSD, GBPJPY)",
                },
                "all_etf_quotes": {
                    "description": "Quotes for all ETFs",
                    "required_params": [],
                    "optional_params": ["short", "limit"],
                    "returns": "Array of ETF quotes with price data",
                },
                "all_commodity_quotes": {
                    "description": "Quotes for all commodities",
                    "required_params": [],
                    "optional_params": ["short", "limit"],
                    "returns": "Array of commodity quotes with price data",
                },
                "all_crypto_quotes": {
                    "description": "Quotes for all cryptocurrencies",
                    "required_params": [],
                    "optional_params": ["short", "limit"],
                    "returns": "Array of crypto quotes with price, volume, and market cap",
                },
                "all_forex_quotes": {
                    "description": "Quotes for all forex currency pairs",
                    "required_params": [],
                    "optional_params": ["short", "limit"],
                    "returns": "Array of forex quotes with exchange rates",
                },
                "all_mutualfund_quotes": {
                    "description": "Quotes for all mutual funds",
                    "required_params": [],
                    "optional_params": ["short", "limit"],
                    "returns": "Array of mutual fund quotes with NAV data",
                },
                "all_index_quotes": {
                    "description": "Quotes for all market indexes",
                    "required_params": [],
                    "optional_params": ["short", "limit"],
                    "returns": "Array of index quotes with price and change data",
                },
                "ipo_calendar": {
                    "description": "Upcoming and recent IPO calendar",
                    "required_params": [],
                    "optional_params": ["from_date", "to_date"],
                    "returns": "Array with: date, company, symbol, exchange, actions, shares, priceRange, marketCap",
                },
            },
        ).model_dump()

    provider = get_provider()

    # ETFs
    if request.action == "etf_list":
        return await provider.get_all_etfs(request.limit)
    elif request.action == "etf_profile":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_etf_info(request.symbol)
    elif request.action == "etf_holdings":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_etf_holdings(request.symbol)
    elif request.action == "etf_sector_weightings":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_etf_sector_weightings(request.symbol)
    elif request.action == "etf_country_weightings":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_etf_country_weightings(request.symbol)
    elif request.action == "etf_exposure":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_etf_asset_exposure(request.symbol)
    # Mutual funds
    elif request.action == "mutual_fund_search":
        if not request.query:
            return {"error": "Missing required parameter: query"}
        return await provider.search_fund_disclosure_by_name(request.query, request.limit)
    elif request.action == "fund_disclosure":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        if not request.year:
            return {"error": "Missing required parameter: year"}
        if not request.quarter:
            return {"error": "Missing required parameter: quarter"}
        return await provider.get_fund_disclosure(request.symbol, request.year, request.quarter)
    # Commodities
    elif request.action == "commodity_list":
        return await provider.get_commodities_list()
    elif request.action == "all_commodity_quotes":
        return await provider.get_all_commodity_quotes(request.short, request.limit)
    # Crypto
    elif request.action == "crypto_list":
        return await provider.get_cryptocurrency_list()
    elif request.action == "all_crypto_quotes":
        return await provider.get_all_crypto_quotes(request.short, request.limit)
    # Forex
    elif request.action == "forex_list":
        return await provider.get_forex_currency_pairs()
    elif request.action == "all_forex_quotes":
        return await provider.get_all_forex_quotes(request.short, request.limit)
    # All quotes
    elif request.action == "all_etf_quotes":
        return await provider.get_all_etf_quotes(request.short, request.limit)
    elif request.action == "all_mutualfund_quotes":
        return await provider.get_all_mutualfund_quotes(request.short, request.limit)
    elif request.action == "all_index_quotes":
        return await provider.get_all_index_quotes(request.short, request.limit)
    # IPO
    elif request.action == "ipo_calendar":
        return await provider.get_ipos_calendar(request.from_date, request.to_date)

    return {"error": f"Unknown action: {request.action}"}


async def fmp_news(request: NewsInput) -> dict:
    """Financial news and press releases with pagination and date filtering. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_news",
            description="Financial news and press releases",
            actions={
                "stock_news": {
                    "description": "Stock market news. Filter by symbol or get latest news.",
                    "required_params": [],
                    "optional_params": [
                        "symbol",
                        "symbols",
                        "page",
                        "limit",
                        "from_date",
                        "to_date",
                    ],
                    "returns": "Array with: title, date, content, tickers, image, link, author, site",
                },
                "forex_news": {
                    "description": "Forex/currency market news. Filter by pair or get latest.",
                    "required_params": [],
                    "optional_params": [
                        "symbol",
                        "symbols",
                        "page",
                        "limit",
                        "from_date",
                        "to_date",
                    ],
                    "returns": "Array with: title, date, content, tickers, image, link, author, site",
                },
                "crypto_news": {
                    "description": "Cryptocurrency news. Filter by symbol or get latest.",
                    "required_params": [],
                    "optional_params": [
                        "symbol",
                        "symbols",
                        "page",
                        "limit",
                        "from_date",
                        "to_date",
                    ],
                    "returns": "Array with: title, date, content, tickers, image, link, author, site",
                },
                "general_news": {
                    "description": "General financial and economic news (not symbol-specific)",
                    "required_params": [],
                    "optional_params": ["page", "limit", "from_date", "to_date"],
                    "returns": "Array with: title, date, content, image, link, author, site",
                },
                "press_releases": {
                    "description": "Company press releases from all companies",
                    "required_params": [],
                    "optional_params": ["page", "limit", "from_date", "to_date"],
                    "returns": "Array with: symbol, date, title, text",
                },
                "press_releases_by_symbol": {
                    "description": "Press releases for a specific company or companies. Requires at least one of symbol or symbols.",
                    "required_params": [],
                    "optional_params": [
                        "symbol",
                        "symbols",
                        "page",
                        "limit",
                        "from_date",
                        "to_date",
                    ],
                    "returns": "Array with: symbol, date, title, text",
                },
            },
        ).model_dump()

    provider = get_provider()

    if request.action == "stock_news":
        # Use symbol/symbols search if provided, otherwise get latest
        symbols_param = request.symbols or request.symbol
        if symbols_param:
            return await provider.search_stock_news_by_symbol(
                symbols_param.strip(),
                request.page or 0,
                request.limit,
                request.from_date,
                request.to_date,
            )
        return await provider.get_stock_news_latest(
            request.page or 0,
            request.limit,
            request.from_date,
            request.to_date,
        )
    elif request.action == "forex_news":
        # Use symbol/symbols search if provided, otherwise get latest
        symbols_param = request.symbols or request.symbol
        if symbols_param:
            return await provider.search_forex_news_by_symbol(
                symbols_param.strip(),
                request.page or 0,
                request.limit,
                request.from_date,
                request.to_date,
            )
        return await provider.get_forex_news_latest(
            request.page or 0, request.limit, request.from_date, request.to_date
        )
    elif request.action == "crypto_news":
        # Use symbol/symbols search if provided, otherwise get latest
        symbols_param = request.symbols or request.symbol
        if symbols_param:
            return await provider.search_crypto_news_by_symbol(
                symbols_param.strip(),
                request.page or 0,
                request.limit,
                request.from_date,
                request.to_date,
            )
        return await provider.get_crypto_news_latest(
            request.page or 0, request.limit, request.from_date, request.to_date
        )
    elif request.action == "general_news":
        return await provider.get_general_news_latest(
            request.page or 0, request.limit, request.from_date, request.to_date
        )
    elif request.action == "press_releases":
        return await provider.get_press_releases_latest(
            request.page or 0, request.limit, request.from_date, request.to_date
        )
    elif request.action == "press_releases_by_symbol":
        symbols_param = request.symbols or request.symbol
        if not symbols_param:
            return {"error": "Missing required parameter: symbol or symbols"}
        return await provider.search_press_releases_by_symbol(
            symbols_param.strip(),
            request.page or 0,
            request.limit,
            request.from_date,
            request.to_date,
        )

    return {"error": f"Unknown action: {request.action}"}


async def fmp_government(request: GovernmentInput) -> dict:
    """Congressional trading disclosures (House/Senate) and SEC 8-K filings. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_government",
            description="Congressional trading and SEC filings data",
            actions={
                "house_disclosure": {
                    "description": "House of Representatives financial disclosures (STOCK Act)",
                    "required_params": [],
                    "optional_params": ["page", "limit"],
                    "returns": "Array with: disclosureDate, transactionDate, owner, ticker, assetDescription, type, amount, representative, district, link",
                },
                "senate_disclosure": {
                    "description": "Senate financial disclosures (STOCK Act)",
                    "required_params": [],
                    "optional_params": ["page", "limit"],
                    "returns": "Array with: disclosureDate, transactionDate, owner, ticker, assetDescription, type, amount, senator, link",
                },
                "house_trades": {
                    "description": "House trading activity filtered by stock symbol",
                    "required_params": ["symbol"],
                    "optional_params": ["page", "limit"],
                    "returns": "Array of House trades for the specified stock",
                },
                "senate_trades": {
                    "description": "Senate trading activity filtered by stock symbol",
                    "required_params": ["symbol"],
                    "optional_params": ["page", "limit"],
                    "returns": "Array of Senate trades for the specified stock",
                },
                "institutional_ownership": {
                    "description": "Latest institutional ownership 13F filings",
                    "required_params": [],
                    "optional_params": ["page", "limit"],
                    "returns": "Array of institutional ownership filings with: cik, filingDate, investor, shares, value",
                },
                "filings_8k": {
                    "description": "Latest 8-K SEC filings (material events)",
                    "required_params": [],
                    "optional_params": ["page", "limit"],
                    "returns": "Array with: symbol, cik, formType, acceptedDate, filingDate, link",
                },
            },
        ).model_dump()

    provider = get_provider()

    if request.action == "house_disclosure":
        return await provider.get_house_disclosure(request.page, request.limit)
    elif request.action == "senate_disclosure":
        return await provider.get_senate_disclosure(request.page, request.limit)
    elif request.action == "house_trades":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_house_trades(request.symbol, request.page, request.limit)
    elif request.action == "senate_trades":
        if not request.symbol:
            return {"error": "Missing required parameter: symbol"}
        return await provider.get_senate_trades(request.symbol, request.page, request.limit)
    elif request.action == "institutional_ownership":
        return await provider.get_institutional_ownership(request.page, request.limit)
    elif request.action == "filings_8k":
        return await provider.get_8k_filings(request.page, request.limit)

    return {"error": f"Unknown action: {request.action}"}


async def fmp_technical(request: TechnicalInput) -> dict:
    """Technical indicators (SMA, EMA, RSI, ADX, etc.) for stocks, ETFs, crypto, forex. Call with action='help' for available actions."""
    if request.action == "help":
        return HelpResponse(
            tool_name="fmp_technical",
            description="Technical analysis indicators",
            actions={
                "sma": {
                    "description": "Simple Moving Average - arithmetic mean of prices over N periods",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, sma",
                },
                "ema": {
                    "description": "Exponential Moving Average - weighted average with more weight on recent prices",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, ema",
                },
                "wma": {
                    "description": "Weighted Moving Average - linearly weighted average",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, wma",
                },
                "dema": {
                    "description": "Double Exponential Moving Average - reduces lag vs standard EMA",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, dema",
                },
                "tema": {
                    "description": "Triple Exponential Moving Average - further reduces lag vs DEMA",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, tema",
                },
                "williams": {
                    "description": "Williams %R - momentum indicator (-100 to 0, oversold below -80, overbought above -20)",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, williams",
                },
                "rsi": {
                    "description": "Relative Strength Index - momentum oscillator (0-100, oversold below 30, overbought above 70)",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, rsi",
                },
                "adx": {
                    "description": "Average Directional Index - trend strength indicator (0-100, strong trend above 25)",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, adx",
                },
                "standard_deviation": {
                    "description": "Standard Deviation - volatility measure over N periods",
                    "required_params": ["symbol"],
                    "optional_params": ["period", "interval"],
                    "returns": "Array with: date, open, high, low, close, standardDeviation",
                },
            },
        ).model_dump()

    provider = get_provider()

    # All technical indicator actions require symbol
    if request.action != "help" and not request.symbol:
        return {"error": "Missing required parameter: symbol"}

    if request.action == "sma":
        return await provider.get_sma(request.symbol, request.period, request.interval)
    elif request.action == "ema":
        return await provider.get_ema(request.symbol, request.period, request.interval)
    elif request.action == "wma":
        return await provider.get_wma(request.symbol, request.period, request.interval)
    elif request.action == "dema":
        return await provider.get_dema(request.symbol, request.period, request.interval)
    elif request.action == "tema":
        return await provider.get_tema(request.symbol, request.period, request.interval)
    elif request.action == "williams":
        return await provider.get_williams(request.symbol, request.period, request.interval)
    elif request.action == "rsi":
        return await provider.get_rsi(request.symbol, request.period, request.interval)
    elif request.action == "adx":
        return await provider.get_adx(request.symbol, request.period, request.interval)
    elif request.action == "standard_deviation":
        return await provider.get_standard_deviation(
            request.symbol, request.period, request.interval
        )

    return {"error": f"Unknown action: {request.action}"}


async def fmp_schema(request: SchemaInput) -> dict:
    """Introspect meta-tool input schemas; omit tool_name to list all available tools."""
    meta_tools: dict[str, Any] = {
        "fmp_analyst": {
            "description": "Analyst ratings, estimates, grades, and price targets",
            "input_model": AnalystInput,
        },
        "fmp_prices": {
            "description": "Stock quotes, historical prices, and intraday data",
            "input_model": PricesInput,
        },
        "fmp_company": {
            "description": "Company profiles, search, and stock directory",
            "input_model": CompanyInput,
        },
        "fmp_financials": {
            "description": "Financial statements, valuations, earnings, and dividends",
            "input_model": FinancialsInput,
        },
        "fmp_market": {
            "description": "Market performance, indexes, and economic data",
            "input_model": MarketInput,
        },
        "fmp_assets": {
            "description": "ETFs, mutual funds, commodities, crypto, and forex",
            "input_model": AssetsInput,
        },
        "fmp_news": {
            "description": "Financial news and press releases",
            "input_model": NewsInput,
        },
        "fmp_government": {
            "description": "Congressional trading and SEC filings",
            "input_model": GovernmentInput,
        },
        "fmp_technical": {
            "description": "Technical analysis indicators",
            "input_model": TechnicalInput,
        },
        "fmp_schema": {
            "description": "Tool introspection - get schemas for any meta-tool",
            "input_model": SchemaInput,
        },
    }

    if request.tool_name is None:
        return {
            "available_tools": list(meta_tools.keys()),
            "usage": "Call with tool_name to get detailed schema",
        }

    if request.tool_name not in meta_tools:
        return {"error": f"Unknown tool: {request.tool_name}"}

    tool_info = meta_tools[request.tool_name]
    # Sanitize schema for Gemini compatibility (removes $defs, resolves $ref)
    raw_schema = tool_info["input_model"].model_json_schema()
    sanitized_schema = flatten_schema(raw_schema)
    return {
        "tool": request.tool_name,
        "description": tool_info["description"],
        "input_schema": sanitized_schema,
    }
