"""FMP API endpoint definitions for fixture downloads.

This module defines all the FMP API endpoints needed to populate
the offline fixtures database for the 173 implemented tools.
"""

from dataclasses import dataclass
from enum import Enum


class EndpointCategory(str, Enum):
    """Categories of API endpoints."""

    QUOTE = "quote"
    PROFILE = "profile"
    FINANCIAL = "financial"
    HISTORICAL = "historical"
    TECHNICAL = "technical"
    ANALYST = "analyst"
    MARKET = "market"
    OTHER = "other"


@dataclass
class Endpoint:
    """Definition of an FMP API endpoint."""

    path: str
    category: EndpointCategory
    description: str
    requires_symbol: bool = True
    params: dict | None = None


# Default 25 symbols from the build plan
DEFAULT_SYMBOLS = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "NVDA",
    "TSLA",
    "BRK.B",
    "JPM",
    "V",
    "JNJ",
    "UNH",
    "XOM",
    "PG",
    "HD",
    "MA",
    "CVX",
    "MRK",
    "ABBV",
    "PEP",
    "KO",
    "COST",
    "AVGO",
    "WMT",
    "LLY",
]


# Symbol-specific endpoints (called per symbol)
SYMBOL_ENDPOINTS = [
    # Quotes
    Endpoint("/quote", EndpointCategory.QUOTE, "Stock quote"),
    Endpoint("/quote-short", EndpointCategory.QUOTE, "Stock quote short"),
    Endpoint("/aftermarket-trade", EndpointCategory.QUOTE, "Aftermarket trade"),
    Endpoint("/aftermarket-quote", EndpointCategory.QUOTE, "Aftermarket quote"),
    Endpoint("/stock-price-change", EndpointCategory.QUOTE, "Stock price change"),
    # Company Information
    Endpoint("/profile", EndpointCategory.PROFILE, "Company profile"),
    Endpoint("/company-notes", EndpointCategory.PROFILE, "Company notes"),
    Endpoint("/stock-peers", EndpointCategory.PROFILE, "Stock peers"),
    Endpoint("/stock_peers", EndpointCategory.PROFILE, "Stock peers (alt)"),
    Endpoint("/employee-count", EndpointCategory.PROFILE, "Employee count"),
    Endpoint("/historical-employee-count", EndpointCategory.PROFILE, "Historical employee count"),
    Endpoint(
        "/historical/employee_count", EndpointCategory.PROFILE, "Historical employee count (alt)"
    ),
    Endpoint("/market-capitalization", EndpointCategory.PROFILE, "Market capitalization"),
    Endpoint(
        "/historical-market-capitalization", EndpointCategory.PROFILE, "Historical market cap"
    ),
    Endpoint("/shares-float", EndpointCategory.PROFILE, "Shares float"),
    Endpoint("/shares_float", EndpointCategory.PROFILE, "Shares float (alt)"),
    Endpoint("/key-executives", EndpointCategory.PROFILE, "Company executives"),
    Endpoint(
        "/governance-executive-compensation", EndpointCategory.PROFILE, "Executive compensation"
    ),
    Endpoint(
        "/governance/executive-compensation",
        EndpointCategory.PROFILE,
        "Executive compensation (alt)",
    ),
    Endpoint(
        "/executive-compensation-benchmark",
        EndpointCategory.PROFILE,
        "Executive compensation benchmark",
    ),
    # Financial Statements
    Endpoint(
        "/income-statement",
        EndpointCategory.FINANCIAL,
        "Income statement (annual)",
        params={"period": "annual", "limit": 5},
    ),
    Endpoint(
        "/income-statement",
        EndpointCategory.FINANCIAL,
        "Income statement (quarterly)",
        params={"period": "quarter", "limit": 8},
    ),
    Endpoint(
        "/balance-sheet-statement",
        EndpointCategory.FINANCIAL,
        "Balance sheet (annual)",
        params={"period": "annual", "limit": 5},
    ),
    Endpoint(
        "/balance-sheet-statement",
        EndpointCategory.FINANCIAL,
        "Balance sheet (quarterly)",
        params={"period": "quarter", "limit": 8},
    ),
    Endpoint(
        "/cash-flow-statement",
        EndpointCategory.FINANCIAL,
        "Cash flow (annual)",
        params={"period": "annual", "limit": 5},
    ),
    Endpoint(
        "/cash-flow-statement",
        EndpointCategory.FINANCIAL,
        "Cash flow (quarterly)",
        params={"period": "quarter", "limit": 8},
    ),
    Endpoint("/key-metrics", EndpointCategory.FINANCIAL, "Key metrics", params={"limit": 5}),
    Endpoint("/ratios", EndpointCategory.FINANCIAL, "Financial ratios", params={"limit": 5}),
    Endpoint(
        "/financial-growth",
        EndpointCategory.FINANCIAL,
        "Financial growth",
        params={"limit": 5},
    ),
    Endpoint("/financial-reports-dates", EndpointCategory.FINANCIAL, "Financial report dates"),
    Endpoint(
        "/key-metrics-ttm",
        EndpointCategory.FINANCIAL,
        "Key metrics TTM",
    ),
    Endpoint("/ratios-ttm", EndpointCategory.FINANCIAL, "Financial ratios TTM"),
    # Financial statements as reported
    Endpoint(
        "/income-statement-as-reported",
        EndpointCategory.FINANCIAL,
        "Income statement as reported",
        params={"period": "annual", "limit": 5},
    ),
    Endpoint(
        "/balance-sheet-statement-as-reported",
        EndpointCategory.FINANCIAL,
        "Balance sheet as reported",
        params={"period": "annual", "limit": 5},
    ),
    Endpoint(
        "/cash-flow-statement-as-reported",
        EndpointCategory.FINANCIAL,
        "Cash flow as reported",
        params={"period": "annual", "limit": 5},
    ),
    Endpoint(
        "/financial-statement-full-as-reported",
        EndpointCategory.FINANCIAL,
        "Full financials as reported",
        params={"period": "annual", "limit": 5},
    ),
    Endpoint("/financial-reports-json", EndpointCategory.FINANCIAL, "Financial report JSON"),
    Endpoint(
        "/enterprise-values", EndpointCategory.FINANCIAL, "Enterprise values", params={"limit": 5}
    ),
    Endpoint("/owner-earnings", EndpointCategory.FINANCIAL, "Owner earnings", params={"limit": 5}),
    # Revenue segmentation
    Endpoint("/revenue-product-segmentation", EndpointCategory.FINANCIAL, "Revenue by product"),
    Endpoint(
        "/revenue-geographic-segmentation", EndpointCategory.FINANCIAL, "Revenue by geography"
    ),
    # Historical Prices
    Endpoint(
        "/historical-price-eod/full",
        EndpointCategory.HISTORICAL,
        "Historical daily prices",
        params={"serietype": "line"},
    ),
    Endpoint(
        "/historical-price-eod/dividend-adjusted",
        EndpointCategory.HISTORICAL,
        "Dividend-adjusted historical prices (adjOpen, adjHigh, adjLow, adjClose)",
    ),
    Endpoint(
        "/historical-price-eod/dividend",
        EndpointCategory.HISTORICAL,
        "Dividend history",
    ),
    Endpoint(
        "/historical-price-eod/stock_split",
        EndpointCategory.HISTORICAL,
        "Stock split history",
    ),
    # Intraday charts
    Endpoint("/historical-chart/1min", EndpointCategory.HISTORICAL, "Intraday 1-min chart"),
    Endpoint("/historical-chart/5min", EndpointCategory.HISTORICAL, "Intraday 5-min chart"),
    Endpoint("/historical-chart/15min", EndpointCategory.HISTORICAL, "Intraday 15-min chart"),
    Endpoint("/historical-chart/30min", EndpointCategory.HISTORICAL, "Intraday 30-min chart"),
    Endpoint("/historical-chart/1hour", EndpointCategory.HISTORICAL, "Intraday 1-hour chart"),
    Endpoint("/historical-chart/4hour", EndpointCategory.HISTORICAL, "Intraday 4-hour chart"),
    # Analyst
    Endpoint(
        "/analyst-estimates",
        EndpointCategory.ANALYST,
        "Analyst estimates",
        params={"period": "annual", "page": 0, "limit": 10},
    ),
    Endpoint("/rating", EndpointCategory.ANALYST, "Company rating snapshot"),
    Endpoint(
        "/historical-rating", EndpointCategory.ANALYST, "Historical rating", params={"limit": 10}
    ),
    Endpoint("/grade", EndpointCategory.ANALYST, "Stock grade"),
    Endpoint(
        "/grades",
        EndpointCategory.ANALYST,
        "Stock recommendations",
    ),
    Endpoint(
        "/grades-consensus",
        EndpointCategory.ANALYST,
        "Upgrades/downgrades consensus",
    ),
    Endpoint(
        "/grades-historical",
        EndpointCategory.ANALYST,
        "Historical grades",
        params={"limit": 10},
    ),
    Endpoint("/price-target-consensus", EndpointCategory.ANALYST, "Price target consensus"),
    Endpoint("/price-target-summary", EndpointCategory.ANALYST, "Price target summary"),
    Endpoint(
        "/price-target-rss-feed",
        EndpointCategory.ANALYST,
        "Price target news feed",
        params={"limit": 20},
    ),
    Endpoint(
        "/upgrades-downgrades-rss-feed",
        EndpointCategory.ANALYST,
        "Grade changes news feed",
        params={"limit": 20},
    ),
    # Earnings
    Endpoint("/earnings", EndpointCategory.OTHER, "Earnings report"),
    Endpoint("/historical/earning_calendar", EndpointCategory.OTHER, "Historical earnings"),
    # Earnings transcripts
    Endpoint("/earning-call-transcript", EndpointCategory.OTHER, "Earning call transcript"),
    Endpoint("/earning_call_transcript", EndpointCategory.OTHER, "Earning call transcript (alt)"),
    Endpoint("/earning_call_transcript/dates", EndpointCategory.OTHER, "Transcript dates"),
    Endpoint("/earning_call_transcript/latest", EndpointCategory.OTHER, "Latest transcripts"),
    # ETF/Fund data
    Endpoint("/etf-holdings", EndpointCategory.OTHER, "ETF holdings"),
    Endpoint("/etf-info", EndpointCategory.OTHER, "ETF information"),
    Endpoint("/etf-sector-weightings", EndpointCategory.OTHER, "ETF sector weightings"),
    Endpoint("/etf-country-weightings", EndpointCategory.OTHER, "ETF country weightings"),
    Endpoint("/etf-stock-exposure", EndpointCategory.OTHER, "ETF stock exposure"),
    # Mutual fund disclosure
    Endpoint("/mutual-fund-holdings", EndpointCategory.OTHER, "Mutual fund holdings"),
    Endpoint("/mutual-fund-holdings/dates", EndpointCategory.OTHER, "Fund disclosure dates"),
    Endpoint("/mutual-fund-holdings/latest", EndpointCategory.OTHER, "Latest fund holdings"),
    # SEC Filings
    Endpoint(
        "/sec-filings-search/symbol",
        EndpointCategory.OTHER,
        "SEC filings",
        params={"limit": 20, "from": "2024-01-01", "to": "2025-01-01"},
    ),
    # Insider Trading
    Endpoint(
        "/insider-trading/statistics",
        EndpointCategory.OTHER,
        "Insider trading",
    ),
    # Institutional Holdings
    Endpoint(
        "/institutional-ownership/symbol-positions-summary",
        EndpointCategory.OTHER,
        "Institutional holders",
    ),
    # Technical Indicators (note: plural "indicators" in API path)
    Endpoint(
        "/technical-indicators/sma",
        EndpointCategory.TECHNICAL,
        "SMA indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/ema",
        EndpointCategory.TECHNICAL,
        "EMA indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/rsi",
        EndpointCategory.TECHNICAL,
        "RSI indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/wma",
        EndpointCategory.TECHNICAL,
        "WMA indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/dema",
        EndpointCategory.TECHNICAL,
        "DEMA indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/tema",
        EndpointCategory.TECHNICAL,
        "TEMA indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/williams",
        EndpointCategory.TECHNICAL,
        "Williams %R indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/adx",
        EndpointCategory.TECHNICAL,
        "ADX indicator",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    Endpoint(
        "/technical-indicators/standardDeviation",
        EndpointCategory.TECHNICAL,
        "Standard deviation",
        params={"periodLength": 14, "timeframe": "1day"},
    ),
    # Valuation
    Endpoint("/discounted-cash-flow", EndpointCategory.OTHER, "DCF valuation"),
    Endpoint("/levered-discounted-cash-flow", EndpointCategory.OTHER, "Levered DCF"),
    Endpoint("/advanced-discounted-cash-flow", EndpointCategory.OTHER, "Advanced DCF"),
    # News
    Endpoint("/news/stock", EndpointCategory.OTHER, "Stock news", params={"limit": 20}),
    Endpoint("/press-releases", EndpointCategory.OTHER, "Press releases", params={"limit": 20}),
    # FMP Articles
    Endpoint(
        "/fmp/articles", EndpointCategory.OTHER, "FMP articles", params={"page": 0, "size": 20}
    ),
]

# Non-symbol endpoints (called once)
NON_SYMBOL_ENDPOINTS = [
    # Market Data / Stock Lists
    Endpoint("/stock-list", EndpointCategory.MARKET, "Stock list", requires_symbol=False),
    Endpoint("/etf-list", EndpointCategory.MARKET, "ETF list", requires_symbol=False),
    Endpoint("/index-list", EndpointCategory.MARKET, "Available indexes", requires_symbol=False),
    Endpoint(
        "/commodities-list", EndpointCategory.MARKET, "Commodities list", requires_symbol=False
    ),
    Endpoint(
        "/cryptocurrencies-list",
        EndpointCategory.MARKET,
        "Cryptocurrencies list",
        requires_symbol=False,
    ),
    Endpoint(
        "/forex-pairs-list", EndpointCategory.MARKET, "Forex pairs list", requires_symbol=False
    ),
    Endpoint(
        "/delisted-companies",
        EndpointCategory.MARKET,
        "Delisted companies",
        requires_symbol=False,
        params={"limit": 100},
    ),
    Endpoint(
        "/symbol_change", EndpointCategory.MARKET, "Recent symbol changes", requires_symbol=False
    ),
    # Index constituents
    Endpoint(
        "/sp500-constituent", EndpointCategory.MARKET, "S&P 500 constituents", requires_symbol=False
    ),
    Endpoint(
        "/sp500_constituent",
        EndpointCategory.MARKET,
        "S&P 500 constituents (alt)",
        requires_symbol=False,
    ),
    Endpoint(
        "/nasdaq_constituent", EndpointCategory.MARKET, "NASDAQ constituents", requires_symbol=False
    ),
    Endpoint(
        "/dowjones_constituent",
        EndpointCategory.MARKET,
        "Dow Jones constituents",
        requires_symbol=False,
    ),
    Endpoint(
        "/historical/sp500_constituent",
        EndpointCategory.MARKET,
        "Historical S&P 500",
        requires_symbol=False,
    ),
    Endpoint(
        "/historical/nasdaq_constituent",
        EndpointCategory.MARKET,
        "Historical NASDAQ",
        requires_symbol=False,
    ),
    Endpoint(
        "/historical/dowjones_constituent",
        EndpointCategory.MARKET,
        "Historical Dow Jones",
        requires_symbol=False,
    ),
    # Market Performance
    Endpoint(
        "/sector-performance-snapshot",
        EndpointCategory.MARKET,
        "Sector performance snapshot",
        requires_symbol=False,
    ),
    Endpoint(
        "/sectors-performance", EndpointCategory.MARKET, "Sector performance", requires_symbol=False
    ),
    Endpoint(
        "/industries-performance",
        EndpointCategory.MARKET,
        "Industry performance",
        requires_symbol=False,
    ),
    Endpoint(
        "/historical-sectors-performance",
        EndpointCategory.MARKET,
        "Historical sector performance",
        requires_symbol=False,
    ),
    Endpoint(
        "/historical-industries-performance",
        EndpointCategory.MARKET,
        "Historical industry performance",
        requires_symbol=False,
    ),
    Endpoint(
        "/sector_price_earning_ratio",
        EndpointCategory.MARKET,
        "Sector P/E ratio",
        requires_symbol=False,
    ),
    Endpoint(
        "/industry_price_earning_ratio",
        EndpointCategory.MARKET,
        "Industry P/E ratio",
        requires_symbol=False,
    ),
    Endpoint(
        "/historical-sector-price-earning-ratio",
        EndpointCategory.MARKET,
        "Historical sector P/E",
        requires_symbol=False,
    ),
    Endpoint(
        "/historical-industry-price-earning-ratio",
        EndpointCategory.MARKET,
        "Historical industry P/E",
        requires_symbol=False,
    ),
    Endpoint("/biggest-gainers", EndpointCategory.MARKET, "Biggest gainers", requires_symbol=False),
    Endpoint("/biggest-losers", EndpointCategory.MARKET, "Biggest losers", requires_symbol=False),
    Endpoint("/most-actives", EndpointCategory.MARKET, "Most active", requires_symbol=False),
    # Market Hours
    Endpoint(
        "/exchange-market-hours",
        EndpointCategory.MARKET,
        "Exchange market hours",
        requires_symbol=False,
        params={"exchange": "NASDAQ"},
    ),
    Endpoint("/market-hours", EndpointCategory.MARKET, "Market hours", requires_symbol=False),
    Endpoint("/market-holidays", EndpointCategory.MARKET, "Market holidays", requires_symbol=False),
    # Batch quotes
    Endpoint("/batch-index-quotes", EndpointCategory.MARKET, "Index quotes", requires_symbol=False),
    Endpoint(
        "/batch-commodity-quotes",
        EndpointCategory.MARKET,
        "Commodity quotes",
        requires_symbol=False,
    ),
    Endpoint(
        "/batch-crypto-quotes", EndpointCategory.MARKET, "Crypto quotes", requires_symbol=False
    ),
    Endpoint("/batch-forex-quotes", EndpointCategory.MARKET, "Forex quotes", requires_symbol=False),
    Endpoint(
        "/batch-mutualfund-quotes",
        EndpointCategory.MARKET,
        "Mutual fund quotes",
        requires_symbol=False,
    ),
    Endpoint(
        "/batch-aftermarket-trade",
        EndpointCategory.MARKET,
        "Batch aftermarket trades",
        requires_symbol=False,
    ),
    Endpoint(
        "/batch-aftermarket-quote",
        EndpointCategory.MARKET,
        "Batch aftermarket quotes",
        requires_symbol=False,
    ),
    # Shares float
    Endpoint(
        "/shares_float/all", EndpointCategory.MARKET, "All shares float", requires_symbol=False
    ),
    # Economic Data
    Endpoint("/treasury-rates", EndpointCategory.OTHER, "Treasury rates", requires_symbol=False),
    Endpoint("/treasury", EndpointCategory.OTHER, "Treasury rates (alt)", requires_symbol=False),
    Endpoint(
        "/market_risk_premium", EndpointCategory.OTHER, "Market risk premium", requires_symbol=False
    ),
    Endpoint(
        "/economic-indicators",
        EndpointCategory.OTHER,
        "Economic indicators (GDP)",
        requires_symbol=False,
        params={"name": "GDP"},
    ),
    Endpoint(
        "/economic-indicators",
        EndpointCategory.OTHER,
        "Economic indicators (unemployment)",
        requires_symbol=False,
        params={"name": "unemploymentRate"},
    ),
    Endpoint(
        "/economic-indicators",
        EndpointCategory.OTHER,
        "Economic indicators (inflation)",
        requires_symbol=False,
        params={"name": "inflationRate"},
    ),
    Endpoint(
        "/economic-indicators",
        EndpointCategory.OTHER,
        "Economic indicators (CPI)",
        requires_symbol=False,
        params={"name": "CPI"},
    ),
    Endpoint(
        "/economic-indicators",
        EndpointCategory.OTHER,
        "Economic indicators (interest rate)",
        requires_symbol=False,
        params={"name": "interestRate"},
    ),
    # Calendars
    Endpoint("/ipos-calendar", EndpointCategory.OTHER, "IPO calendar", requires_symbol=False),
    Endpoint("/ipo_calendar", EndpointCategory.OTHER, "IPO calendar (alt)", requires_symbol=False),
    Endpoint(
        "/ipo-calendar-confirmed", EndpointCategory.OTHER, "Confirmed IPOs", requires_symbol=False
    ),
    Endpoint(
        "/ipo-calendar-prospectus", EndpointCategory.OTHER, "IPO prospectus", requires_symbol=False
    ),
    Endpoint(
        "/earnings-calendar",
        EndpointCategory.OTHER,
        "Earnings calendar",
        requires_symbol=False,
        params={"limit": 100},
    ),
    Endpoint(
        "/earning_calendar",
        EndpointCategory.OTHER,
        "Earnings calendar (alt)",
        requires_symbol=False,
    ),
    Endpoint(
        "/economic-calendar",
        EndpointCategory.OTHER,
        "Economic calendar",
        requires_symbol=False,
        params={"limit": 100},
    ),
    Endpoint(
        "/economic_calendar",
        EndpointCategory.OTHER,
        "Economic calendar (alt)",
        requires_symbol=False,
    ),
    Endpoint(
        "/splits-calendar", EndpointCategory.OTHER, "Stock split calendar", requires_symbol=False
    ),
    Endpoint(
        "/stock_split_calendar",
        EndpointCategory.OTHER,
        "Stock split calendar (alt)",
        requires_symbol=False,
    ),
    Endpoint(
        "/dividends-calendar", EndpointCategory.OTHER, "Dividend calendar", requires_symbol=False
    ),
    Endpoint(
        "/stock_dividend_calendar",
        EndpointCategory.OTHER,
        "Dividend calendar (alt)",
        requires_symbol=False,
    ),
    # News (global)
    Endpoint(
        "/news/general-latest",
        EndpointCategory.OTHER,
        "Latest general news",
        requires_symbol=False,
        params={"limit": 50},
    ),
    Endpoint(
        "/news/stock-latest",
        EndpointCategory.OTHER,
        "Latest stock news",
        requires_symbol=False,
        params={"limit": 50},
    ),
    Endpoint(
        "/news/crypto-latest",
        EndpointCategory.OTHER,
        "Latest crypto news",
        requires_symbol=False,
        params={"limit": 50},
    ),
    Endpoint(
        "/news/crypto",
        EndpointCategory.OTHER,
        "Crypto news",
        requires_symbol=False,
        params={"limit": 50},
    ),
    Endpoint(
        "/news/forex-latest",
        EndpointCategory.OTHER,
        "Latest forex news",
        requires_symbol=False,
        params={"limit": 50},
    ),
    Endpoint(
        "/news/forex",
        EndpointCategory.OTHER,
        "Forex news",
        requires_symbol=False,
        params={"limit": 50},
    ),
    Endpoint(
        "/press-releases-latest",
        EndpointCategory.OTHER,
        "Latest press releases",
        requires_symbol=False,
        params={"limit": 50},
    ),
    # Transcript symbols
    Endpoint(
        "/earning_call_transcript/companies",
        EndpointCategory.OTHER,
        "Companies with transcripts",
        requires_symbol=False,
    ),
    Endpoint(
        "/earning_call_transcript/symbols",
        EndpointCategory.OTHER,
        "Transcript symbols",
        requires_symbol=False,
    ),
    # Fund search
    Endpoint(
        "/mutual-fund-holdings/search",
        EndpointCategory.OTHER,
        "Search fund disclosure",
        requires_symbol=False,
    ),
]


def get_all_symbol_endpoints() -> list[Endpoint]:
    """Get all symbol-specific endpoints."""
    return SYMBOL_ENDPOINTS.copy()


def get_all_non_symbol_endpoints() -> list[Endpoint]:
    """Get all non-symbol endpoints."""
    return NON_SYMBOL_ENDPOINTS.copy()


def get_endpoint_count() -> tuple[int, int]:
    """Return counts of (symbol_endpoints, non_symbol_endpoints)."""
    return len(SYMBOL_ENDPOINTS), len(NON_SYMBOL_ENDPOINTS)
