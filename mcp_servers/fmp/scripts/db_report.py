#!/usr/bin/env python3
"""Database diagnostic report for FMP MCP Server.

Generates detailed reports on data availability to help diagnose
why tools may not be returning data. Includes tool-to-data mappings.

Usage:
    # Full detailed report
    uv run python -m scripts.db_report

    # Summary only
    uv run python -m scripts.db_report --summary

    # Show all tools and their data sources
    uv run python -m scripts.db_report --tools

    # Show tool coverage (which tools have data)
    uv run python -m scripts.db_report --coverage

    # Check specific symbol
    uv run python -m scripts.db_report --symbol AAPL

    # Check specific endpoint
    uv run python -m scripts.db_report --endpoint /quote

    # Export report to file
    uv run python -m scripts.db_report --output report.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from sqlalchemy import distinct, func, select

# Add the parent directory to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_servers.fmp_server.config import get_config
from mcp_servers.fmp_server.db import (
    Company,
    FinancialStatement,
    GenericApiResponse,
    HistoricalPrice,
    StockQuote,
    TechnicalIndicator,
    close_engine,
    get_session,
)
from scripts.endpoints import (
    DEFAULT_SYMBOLS,
    NON_SYMBOL_ENDPOINTS,
    SYMBOL_ENDPOINTS,
)

# All model classes
ALL_MODELS = [
    Company,
    StockQuote,
    HistoricalPrice,
    FinancialStatement,
    TechnicalIndicator,
    GenericApiResponse,
]


class DataSource(str, Enum):
    """Data sources for tools."""

    STOCK_QUOTE = "stock_quotes"
    COMPANY = "companies"
    HISTORICAL_PRICE = "historical_prices"
    FINANCIAL_STATEMENT = "financial_statements"
    TECHNICAL_INDICATOR = "technical_indicators"
    GENERIC_API = "generic_api_responses"


@dataclass
class ToolMapping:
    """Maps a tool to its data source and endpoint."""

    tool_name: str
    category: str
    data_source: DataSource
    endpoint: str | None = None
    requires_symbol: bool = True
    description: str = ""


def discover_tools_from_modules() -> list[ToolMapping]:
    """Dynamically discover all tools from the tools modules."""
    import importlib
    import inspect

    tool_modules = [
        "mcp_servers.fmp_server.tools.analyst",
        "mcp_servers.fmp_server.tools.charts",
        "mcp_servers.fmp_server.tools.commodity",
        "mcp_servers.fmp_server.tools.company_information",
        "mcp_servers.fmp_server.tools.company_search",
        "mcp_servers.fmp_server.tools.crypto",
        "mcp_servers.fmp_server.tools.dcf_valuation",
        "mcp_servers.fmp_server.tools.earnings_dividends_splits",
        "mcp_servers.fmp_server.tools.earnings_transcript",
        "mcp_servers.fmp_server.tools.economics",
        "mcp_servers.fmp_server.tools.etf_mutual_funds",
        "mcp_servers.fmp_server.tools.financial_statements",
        "mcp_servers.fmp_server.tools.forex",
        "mcp_servers.fmp_server.tools.indexes",
        "mcp_servers.fmp_server.tools.market_hours",
        "mcp_servers.fmp_server.tools.market_performance",
        "mcp_servers.fmp_server.tools.news",
        "mcp_servers.fmp_server.tools.quote",
        "mcp_servers.fmp_server.tools.stock_directory",
        "mcp_servers.fmp_server.tools.technical_indicators",
    ]

    discovered_tools = []

    for module_name in tool_modules:
        try:
            module = importlib.import_module(module_name)

            # Extract category from module name
            short_name = module_name.split(".")[-1]
            category_map = {
                "analyst": "Analyst",
                "charts": "Charts",
                "commodity": "Commodity",
                "company_information": "Company Info",
                "company_search": "Company Search",
                "crypto": "Crypto",
                "dcf_valuation": "DCF Valuation",
                "earnings_dividends_splits": "Earnings & Dividends",
                "earnings_transcript": "Earnings Transcript",
                "economics": "Economics",
                "etf_mutual_funds": "ETF & Mutual Funds",
                "financial_statements": "Financial Statements",
                "forex": "Forex",
                "indexes": "Indexes",
                "market_hours": "Market Hours",
                "market_performance": "Market Performance",
                "news": "News",
                "quote": "Stock Quotes",
                "stock_directory": "Stock Directory",
                "technical_indicators": "Technical Indicators",
            }
            category = category_map.get(short_name, short_name.replace("_", " ").title())

            # Find all async functions in the module
            for name, obj in inspect.getmembers(module):
                if (
                    inspect.iscoroutinefunction(obj)
                    and not name.startswith("_")
                    and obj.__module__ == module_name
                ):
                    # Extract description from docstring
                    doc = obj.__doc__ or ""
                    description = doc.split("\n")[0].strip() if doc else ""

                    # Determine data source based on tool name and category
                    data_source, endpoint = infer_data_source(name, category)

                    discovered_tools.append(
                        ToolMapping(
                            tool_name=name,
                            category=category,
                            data_source=data_source,
                            endpoint=endpoint,
                            requires_symbol=True,  # Default, can be refined
                            description=description[:60] + "..."
                            if len(description) > 60
                            else description,
                        )
                    )

        except ImportError as e:
            print(f"Warning: Could not import {module_name}: {e}")

    return discovered_tools


def infer_data_source(tool_name: str, category: str) -> tuple[DataSource, str | None]:
    """Infer the data source and endpoint for a tool based on its name."""

    # Direct table mappings
    if tool_name in [
        "get_stock_quote",
        "get_stock_quote_short",
        "get_batch_stock_quotes",
        "get_batch_stock_quotes_short",
        "get_exchange_stock_quotes",
        "get_all_etf_quotes",
    ]:
        return DataSource.STOCK_QUOTE, None

    if tool_name in [
        "get_company_profile",
        "get_profile_by_cik",
        "search_by_symbol",
        "search_by_company_name",
        "search_by_cik",
        "search_by_cusip",
        "search_by_isin",
        "screen_stocks",
        "find_exchange_listings",
    ]:
        return DataSource.COMPANY, None

    if tool_name in [
        "get_historical_price_full",
        "get_historical_price_light",
        "get_historical_price_unadjusted",
        "get_historical_price_dividend_adjusted",
    ]:
        return DataSource.HISTORICAL_PRICE, None

    if tool_name in [
        "get_income_statement",
        "get_balance_sheet",
        "get_cash_flow_statement",
        "get_income_statement_ttm",
        "get_balance_sheet_ttm",
        "get_cash_flow_ttm",
    ]:
        return DataSource.FINANCIAL_STATEMENT, None

    if tool_name in [
        "get_sma",
        "get_ema",
        "get_rsi",
        "get_wma",
        "get_dema",
        "get_tema",
        "get_williams",
        "get_adx",
        "get_standard_deviation",
    ]:
        return DataSource.TECHNICAL_INDICATOR, None

    # Everything else uses generic API responses
    # Map tool names to likely endpoints
    endpoint_mapping = {
        # Analyst endpoints
        "get_analyst_estimates": "/analyst-estimates",
        "get_ratings_snapshot": "/rating",
        "get_ratings_historical": "/historical-rating",
        "get_price_target_summary": "/price-target-summary",
        "get_price_target_consensus": "/price-target-consensus",
        "get_price_target_news": "/price-target-rss-feed",
        "get_price_target_latest_news": "/price-target-rss-feed",
        "get_stock_grades": "/grade",
        "get_grades_historical": "/grades-historical",
        "get_grades_consensus": "/grades-consensus",
        "get_grade_news": "/upgrades-downgrades-rss-feed",
        "get_grade_latest_news": "/upgrades-downgrades-rss-feed",
        # Quote endpoints
        "get_aftermarket_trade": "/aftermarket-trade",
        "get_aftermarket_quote": "/aftermarket-quote",
        "get_stock_price_change": "/stock-price-change",
        "get_all_commodity_quotes": "/batch-commodity-quotes",
        "get_all_crypto_quotes": "/batch-crypto-quotes",
        "get_all_forex_quotes": "/batch-forex-quotes",
        "get_all_mutualfund_quotes": "/batch-mutualfund-quotes",
        "get_all_index_quotes": "/batch-index-quotes",
        # Company info endpoints
        "get_company_notes": "/company-notes",
        "get_stock_peers": "/stock_peers",
        "get_employee_count": "/employee-count",
        "get_historical_employee_count": "/historical/employee_count",
        "get_market_cap": "/market-capitalization",
        "get_historical_market_cap": "/historical-market-capitalization",
        "get_shares_float": "/shares_float",
        "get_all_shares_float": "/shares_float/all",
        "get_company_executives": "/key-executives",
        "get_executive_compensation": "/governance/executive-compensation",
        "get_executive_comp_benchmark": "/executive-compensation-benchmark",
        "get_delisted_companies": "/delisted-companies",
        # Financial endpoints
        "get_key_metrics": "/key-metrics",
        "get_financial_ratios": "/ratios",
        "get_key_metrics_ttm": "/key-metrics-ttm",
        "get_ratios_ttm": "/ratios-ttm",
        "get_financial_growth": "/financial-growth",
        "get_financial_reports_dates": "/financial-reports-dates",
        "get_enterprise_values": "/enterprise-values",
        "get_owner_earnings": "/owner-earnings",
        "get_income_as_reported": "/income-statement-as-reported",
        "get_balance_sheet_as_reported": "/balance-sheet-statement-as-reported",
        "get_cash_flow_as_reported": "/cash-flow-statement-as-reported",
        "get_full_financials_as_reported": "/financial-statement-full-as-reported",
        "get_financial_report_json": "/financial-reports-json",
        "get_revenue_by_product": "/revenue-product-segmentation",
        "get_revenue_by_geography": "/revenue-geographic-segmentation",
        # News endpoints
        "get_fmp_articles": "/fmp/articles",
        "get_stock_news_latest": "/news/stock-latest",
        "search_stock_news_by_symbol": "/news/stock",
        "get_general_news_latest": "/news/general-latest",
        "get_press_releases_latest": "/press-releases-latest",
        "search_press_releases_by_symbol": "/press-releases",
        "get_crypto_news_latest": "/news/crypto-latest",
        "search_crypto_news_by_symbol": "/news/crypto",
        "get_forex_news_latest": "/news/forex-latest",
        "search_forex_news_by_symbol": "/news/forex",
        # Earnings
        "get_company_earnings": "/earnings",
        "get_company_dividends": "/historical-price-eod/dividend",
        "get_stock_splits": "/historical-price-eod/stock_split",
        # Earnings transcripts
        "get_earning_call_transcript": "/earning_call_transcript",
        "get_transcript_dates_by_symbol": "/earning_call_transcript/dates",
        "get_latest_earning_transcripts": "/earning_call_transcript/latest",
        "get_available_transcript_symbols": "/earning_call_transcript/symbols",
        "get_companies_with_transcripts": "/earning_call_transcript/companies",
        # ETF/Fund
        "get_etf_holdings": "/etf-holdings",
        "get_etf_info": "/etf-info",
        "get_etf_sector_weightings": "/etf-sector-weightings",
        "get_etf_country_weightings": "/etf-country-weightings",
        "get_etf_asset_exposure": "/etf-stock-exposure",
        "get_fund_disclosure": "/mutual-fund-holdings",
        "get_fund_disclosure_dates": "/mutual-fund-holdings/dates",
        "get_fund_disclosure_holders_latest": "/mutual-fund-holdings/latest",
        "search_fund_disclosure_by_name": "/mutual-fund-holdings/search",
        # DCF
        "get_dcf_valuation": "/discounted-cash-flow",
        "get_levered_dcf_valuation": "/levered-discounted-cash-flow",
        "get_custom_dcf_valuation": "/advanced-discounted-cash-flow",
        "get_custom_levered_dcf_valuation": "/advanced-discounted-cash-flow",
        # Economics
        "get_treasury_rates": "/treasury-rates",
        "get_economic_indicators": "/economic-indicators",
        "get_economic_calendar": "/economic-calendar",
        "get_market_risk_premium": "/market_risk_premium",
        # Market performance
        "get_sector_performance_snapshot": "/sectors-performance",
        "get_industry_performance_snapshot": "/industries-performance",
        "get_historical_sector_performance": "/historical-sectors-performance",
        "get_historical_industry_performance": "/historical-industries-performance",
        "get_sector_pe_snapshot": "/sector_price_earning_ratio",
        "get_industry_pe_snapshot": "/industry_price_earning_ratio",
        "get_historical_sector_pe": "/historical-sector-price-earning-ratio",
        "get_historical_industry_pe": "/historical-industry-price-earning-ratio",
        "get_biggest_gainers": "/biggest-gainers",
        "get_biggest_losers": "/biggest-losers",
        "get_most_actives": "/most-actives",
        # Calendar
        "get_earnings_calendar": "/earning_calendar",
        "get_dividends_calendar": "/stock_dividend_calendar",
        "get_splits_calendar": "/stock_split_calendar",
        "get_ipos_calendar": "/ipo_calendar",
        "get_ipos_disclosure": "/ipo-calendar-confirmed",
        "get_ipos_prospectus": "/ipo-calendar-prospectus",
        # Market hours
        "get_exchange_market_hours": "/market-hours",
        "get_all_exchange_market_hours": "/market-hours",
        "get_holidays_by_exchange": "/market-holidays",
        # Stock directory
        "get_all_stock_symbols": "/stock-list",
        "get_all_etfs": "/etf-list",
        "get_commodities_list": "/commodities-list",
        "get_cryptocurrency_list": "/cryptocurrencies-list",
        "get_forex_currency_pairs": "/forex-pairs-list",
        "get_available_indexes": "/index-list",
        "get_recent_symbol_changes": "/symbol_change",
        "get_sp500_constituents": "/sp500_constituent",
        "get_nasdaq_constituents": "/nasdaq_constituent",
        "get_dowjones_constituents": "/dowjones_constituent",
        "get_historical_sp500": "/historical/sp500_constituent",
        "get_historical_nasdaq": "/historical/nasdaq_constituent",
        "get_historical_dowjones": "/historical/dowjones_constituent",
        # Intraday charts
        "get_intraday_1min": "/historical-chart/1min",
        "get_intraday_5min": "/historical-chart/5min",
        "get_intraday_15min": "/historical-chart/15min",
        "get_intraday_30min": "/historical-chart/30min",
        "get_intraday_1hour": "/historical-chart/1hour",
        "get_intraday_4hour": "/historical-chart/4hour",
    }

    endpoint = endpoint_mapping.get(tool_name)
    return DataSource.GENERIC_API, endpoint


# Lazy-loaded tool mappings
_TOOL_MAPPINGS: list[ToolMapping] | None = None


def get_tool_mappings() -> list[ToolMapping]:
    """Get all tool mappings, discovering dynamically if needed."""
    global _TOOL_MAPPINGS
    if _TOOL_MAPPINGS is None:
        _TOOL_MAPPINGS = discover_tools_from_modules()
    return _TOOL_MAPPINGS


# Static tool mappings kept for reference (subset of all tools)
_STATIC_TOOL_MAPPINGS: list[ToolMapping] = [
    # Quote Tools - use StockQuote table
    ToolMapping(
        "get_stock_quote",
        "Stock Quotes",
        DataSource.STOCK_QUOTE,
        None,
        True,
        "Real-time stock quote",
    ),
    ToolMapping(
        "get_stock_quote_short",
        "Stock Quotes",
        DataSource.STOCK_QUOTE,
        None,
        True,
        "Condensed stock quote",
    ),
    ToolMapping(
        "get_batch_stock_quotes",
        "Stock Quotes",
        DataSource.STOCK_QUOTE,
        None,
        True,
        "Multiple stock quotes",
    ),
    ToolMapping(
        "get_batch_stock_quotes_short",
        "Stock Quotes",
        DataSource.STOCK_QUOTE,
        None,
        True,
        "Multiple condensed quotes",
    ),
    ToolMapping(
        "get_exchange_stock_quotes",
        "Stock Quotes",
        DataSource.STOCK_QUOTE,
        None,
        False,
        "All quotes on exchange",
    ),
    ToolMapping(
        "get_aftermarket_trade",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/aftermarket-trade",
        True,
        "Aftermarket trading activity",
    ),
    ToolMapping(
        "get_aftermarket_quote",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/aftermarket-quote",
        True,
        "Aftermarket quote data",
    ),
    ToolMapping(
        "get_stock_price_change",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/stock-price-change",
        True,
        "Price changes over periods",
    ),
    ToolMapping(
        "get_batch_aftermarket_trades",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/batch-aftermarket-trade",
        True,
        "Batch aftermarket trades",
    ),
    ToolMapping(
        "get_batch_aftermarket_quotes",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/batch-aftermarket-quote",
        True,
        "Batch aftermarket quotes",
    ),
    ToolMapping(
        "get_all_mutualfund_quotes",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/batch-mutualfund-quotes",
        False,
        "All mutual fund quotes",
    ),
    ToolMapping(
        "get_all_etf_quotes", "Stock Quotes", DataSource.STOCK_QUOTE, None, False, "All ETF quotes"
    ),
    ToolMapping(
        "get_all_commodity_quotes",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/batch-commodity-quotes",
        False,
        "All commodity quotes",
    ),
    ToolMapping(
        "get_all_crypto_quotes",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/batch-crypto-quotes",
        False,
        "All crypto quotes",
    ),
    ToolMapping(
        "get_all_forex_quotes",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/batch-forex-quotes",
        False,
        "All forex quotes",
    ),
    ToolMapping(
        "get_all_index_quotes",
        "Stock Quotes",
        DataSource.GENERIC_API,
        "/batch-index-quotes",
        False,
        "All index quotes",
    ),
    # Company Information Tools - use Company table + GenericApiResponse
    ToolMapping(
        "get_company_profile",
        "Company Info",
        DataSource.COMPANY,
        "/profile",
        True,
        "Company profile",
    ),
    ToolMapping(
        "get_profile_by_cik", "Company Info", DataSource.COMPANY, None, False, "Profile by CIK"
    ),
    ToolMapping(
        "get_company_notes",
        "Company Info",
        DataSource.GENERIC_API,
        "/company-notes",
        True,
        "Company debt notes",
    ),
    ToolMapping(
        "get_stock_peers",
        "Company Info",
        DataSource.GENERIC_API,
        "/stock-peers",
        True,
        "Peer companies",
    ),
    ToolMapping(
        "get_delisted_companies",
        "Company Info",
        DataSource.GENERIC_API,
        "/delisted-companies",
        False,
        "Delisted companies",
    ),
    ToolMapping(
        "get_employee_count",
        "Company Info",
        DataSource.GENERIC_API,
        "/employee-count",
        True,
        "Employee count",
    ),
    ToolMapping(
        "get_historical_employee_count",
        "Company Info",
        DataSource.GENERIC_API,
        "/historical-employee-count",
        True,
        "Employee history",
    ),
    ToolMapping(
        "get_market_cap",
        "Company Info",
        DataSource.GENERIC_API,
        "/market-capitalization",
        True,
        "Market cap",
    ),
    ToolMapping(
        "get_historical_market_cap",
        "Company Info",
        DataSource.GENERIC_API,
        "/historical-market-capitalization",
        True,
        "Historical market cap",
    ),
    ToolMapping(
        "get_shares_float",
        "Company Info",
        DataSource.GENERIC_API,
        "/shares-float",
        True,
        "Shares float",
    ),
    ToolMapping(
        "get_company_executives",
        "Company Info",
        DataSource.GENERIC_API,
        "/key-executives",
        True,
        "Executive info",
    ),
    ToolMapping(
        "get_executive_compensation",
        "Company Info",
        DataSource.GENERIC_API,
        "/governance-executive-compensation",
        True,
        "Exec compensation",
    ),
    # Company Search Tools - use Company table
    ToolMapping(
        "search_by_symbol", "Company Search", DataSource.COMPANY, None, True, "Search by ticker"
    ),
    ToolMapping(
        "search_by_company_name",
        "Company Search",
        DataSource.COMPANY,
        None,
        False,
        "Search by name",
    ),
    ToolMapping(
        "search_by_cik", "Company Search", DataSource.COMPANY, None, False, "Search by CIK"
    ),
    ToolMapping(
        "search_by_cusip", "Company Search", DataSource.COMPANY, None, False, "Search by CUSIP"
    ),
    ToolMapping(
        "search_by_isin", "Company Search", DataSource.COMPANY, None, False, "Search by ISIN"
    ),
    ToolMapping(
        "screen_stocks", "Company Search", DataSource.COMPANY, None, False, "Stock screening"
    ),
    ToolMapping(
        "find_exchange_listings",
        "Company Search",
        DataSource.COMPANY,
        None,
        False,
        "Exchange listings",
    ),
    # Financial Statement Tools - use FinancialStatement table
    ToolMapping(
        "get_income_statement",
        "Financial Statements",
        DataSource.FINANCIAL_STATEMENT,
        None,
        True,
        "Income statement",
    ),
    ToolMapping(
        "get_balance_sheet",
        "Financial Statements",
        DataSource.FINANCIAL_STATEMENT,
        None,
        True,
        "Balance sheet",
    ),
    ToolMapping(
        "get_cash_flow_statement",
        "Financial Statements",
        DataSource.FINANCIAL_STATEMENT,
        None,
        True,
        "Cash flow statement",
    ),
    ToolMapping(
        "get_income_statement_ttm",
        "Financial Statements",
        DataSource.FINANCIAL_STATEMENT,
        None,
        True,
        "Income TTM",
    ),
    ToolMapping(
        "get_balance_sheet_ttm",
        "Financial Statements",
        DataSource.FINANCIAL_STATEMENT,
        None,
        True,
        "Balance sheet TTM",
    ),
    ToolMapping(
        "get_cash_flow_ttm",
        "Financial Statements",
        DataSource.FINANCIAL_STATEMENT,
        None,
        True,
        "Cash flow TTM",
    ),
    ToolMapping(
        "get_key_metrics",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/key-metrics",
        True,
        "Key metrics",
    ),
    ToolMapping(
        "get_financial_ratios",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/ratios",
        True,
        "Financial ratios",
    ),
    ToolMapping(
        "get_key_metrics_ttm",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/key-metrics-ttm",
        True,
        "Key metrics TTM",
    ),
    ToolMapping(
        "get_ratios_ttm",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/ratios-ttm",
        True,
        "Ratios TTM",
    ),
    ToolMapping(
        "get_financial_scores",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/scores",
        True,
        "Financial scores",
    ),
    ToolMapping(
        "get_financial_growth",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/financial-growth",
        True,
        "Financial growth",
    ),
    ToolMapping(
        "get_income_growth",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/income-growth",
        True,
        "Income growth",
    ),
    ToolMapping(
        "get_balance_sheet_growth",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/balance-sheet-growth",
        True,
        "Balance sheet growth",
    ),
    ToolMapping(
        "get_cash_flow_growth",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/cash-flow-growth",
        True,
        "Cash flow growth",
    ),
    ToolMapping(
        "get_financial_reports_dates",
        "Financial Statements",
        DataSource.GENERIC_API,
        "/financial-reports-dates",
        True,
        "Report dates",
    ),
    # Historical Price/Charts Tools - use HistoricalPrice table
    ToolMapping(
        "get_historical_price_full",
        "Charts",
        DataSource.HISTORICAL_PRICE,
        "/historical-price-eod/full",
        True,
        "Full OHLCV history",
    ),
    ToolMapping(
        "get_historical_price_light",
        "Charts",
        DataSource.HISTORICAL_PRICE,
        None,
        True,
        "Light price history",
    ),
    ToolMapping(
        "get_intraday_1hour",
        "Charts",
        DataSource.GENERIC_API,
        "/historical-chart/1hour",
        True,
        "1-hour intraday",
    ),
    ToolMapping(
        "get_intraday_15min",
        "Charts",
        DataSource.GENERIC_API,
        "/historical-chart/15min",
        True,
        "15-min intraday",
    ),
    ToolMapping(
        "get_intraday_5min",
        "Charts",
        DataSource.GENERIC_API,
        "/historical-chart/5min",
        True,
        "5-min intraday",
    ),
    ToolMapping(
        "get_intraday_1min",
        "Charts",
        DataSource.GENERIC_API,
        "/historical-chart/1min",
        True,
        "1-min intraday",
    ),
    # Technical Indicator Tools - use TechnicalIndicator table + GenericApiResponse
    ToolMapping(
        "get_sma",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/sma",
        True,
        "Simple Moving Average",
    ),
    ToolMapping(
        "get_ema",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/ema",
        True,
        "Exponential Moving Average",
    ),
    ToolMapping(
        "get_rsi",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/rsi",
        True,
        "Relative Strength Index",
    ),
    ToolMapping(
        "get_wma",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/wma",
        True,
        "Weighted Moving Average",
    ),
    ToolMapping(
        "get_dema",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/dema",
        True,
        "Double EMA",
    ),
    ToolMapping(
        "get_tema",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/tema",
        True,
        "Triple EMA",
    ),
    ToolMapping(
        "get_williams",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/williams",
        True,
        "Williams %R",
    ),
    ToolMapping(
        "get_adx",
        "Technical Indicators",
        DataSource.TECHNICAL_INDICATOR,
        "/technical-indicators/adx",
        True,
        "Average Directional Index",
    ),
    # Analyst Tools - use GenericApiResponse
    ToolMapping(
        "get_analyst_estimates",
        "Analyst",
        DataSource.GENERIC_API,
        "/analyst-estimates",
        True,
        "Analyst estimates",
    ),
    ToolMapping(
        "get_ratings_snapshot",
        "Analyst",
        DataSource.GENERIC_API,
        "/grades-consensus",
        True,
        "Ratings snapshot",
    ),
    ToolMapping(
        "get_ratings_historical",
        "Analyst",
        DataSource.GENERIC_API,
        "/grades-historical",
        True,
        "Historical ratings",
    ),
    ToolMapping(
        "get_price_target",
        "Analyst",
        DataSource.GENERIC_API,
        "/price-target-consensus",
        True,
        "Price target",
    ),
    ToolMapping(
        "get_price_target_summary",
        "Analyst",
        DataSource.GENERIC_API,
        "/price-target-summary",
        True,
        "Price target summary",
    ),
    # News Tools - use GenericApiResponse
    ToolMapping(
        "search_stock_news_by_symbol",
        "News",
        DataSource.GENERIC_API,
        "/news/stock",
        True,
        "Stock news by symbol",
    ),
    ToolMapping(
        "get_stock_news_latest",
        "News",
        DataSource.GENERIC_API,
        "/news/stock",
        False,
        "Latest stock news",
    ),
    ToolMapping(
        "get_general_news_latest",
        "News",
        DataSource.GENERIC_API,
        "/news/general",
        False,
        "General news",
    ),
    ToolMapping(
        "get_press_releases_latest",
        "News",
        DataSource.GENERIC_API,
        "/press-releases",
        False,
        "Press releases",
    ),
    ToolMapping(
        "get_crypto_news_latest",
        "News",
        DataSource.GENERIC_API,
        "/news/crypto",
        False,
        "Crypto news",
    ),
    ToolMapping(
        "get_forex_news_latest", "News", DataSource.GENERIC_API, "/news/forex", False, "Forex news"
    ),
    # Earnings/Dividends/Splits - use GenericApiResponse
    ToolMapping(
        "get_company_earnings",
        "Earnings & Dividends",
        DataSource.GENERIC_API,
        "/earnings",
        True,
        "Earnings history",
    ),
    ToolMapping(
        "get_earnings_calendar",
        "Earnings & Dividends",
        DataSource.GENERIC_API,
        "/earnings-calendar",
        False,
        "Earnings calendar",
    ),
    ToolMapping(
        "get_company_dividends",
        "Earnings & Dividends",
        DataSource.GENERIC_API,
        "/dividends",
        True,
        "Dividend history",
    ),
    ToolMapping(
        "get_dividends_calendar",
        "Earnings & Dividends",
        DataSource.GENERIC_API,
        "/dividends-calendar",
        False,
        "Dividend calendar",
    ),
    ToolMapping(
        "get_stock_splits",
        "Earnings & Dividends",
        DataSource.GENERIC_API,
        "/stock-splits",
        True,
        "Stock splits",
    ),
    ToolMapping(
        "get_splits_calendar",
        "Earnings & Dividends",
        DataSource.GENERIC_API,
        "/splits-calendar",
        False,
        "Splits calendar",
    ),
    ToolMapping(
        "get_ipos_calendar",
        "Earnings & Dividends",
        DataSource.GENERIC_API,
        "/ipos-calendar",
        False,
        "IPO calendar",
    ),
    # Earnings Transcript Tools - use GenericApiResponse
    ToolMapping(
        "get_earning_call_transcript",
        "Earnings Transcript",
        DataSource.GENERIC_API,
        "/earning-call-transcript",
        True,
        "Earnings transcript",
    ),
    ToolMapping(
        "get_transcript_dates_by_symbol",
        "Earnings Transcript",
        DataSource.GENERIC_API,
        "/earning-call-transcript-dates",
        True,
        "Transcript dates",
    ),
    # DCF Valuation Tools - use GenericApiResponse
    ToolMapping(
        "get_dcf_valuation",
        "DCF Valuation",
        DataSource.GENERIC_API,
        "/discounted-cash-flow",
        True,
        "DCF valuation",
    ),
    ToolMapping(
        "get_levered_dcf_valuation",
        "DCF Valuation",
        DataSource.GENERIC_API,
        "/levered-discounted-cash-flow",
        True,
        "Levered DCF",
    ),
    # Economics Tools - use GenericApiResponse
    ToolMapping(
        "get_treasury_rates",
        "Economics",
        DataSource.GENERIC_API,
        "/treasury-rates",
        False,
        "Treasury rates",
    ),
    ToolMapping(
        "get_economic_indicators",
        "Economics",
        DataSource.GENERIC_API,
        "/economic-indicators",
        False,
        "Economic indicators",
    ),
    ToolMapping(
        "get_economic_calendar",
        "Economics",
        DataSource.GENERIC_API,
        "/economic-calendar",
        False,
        "Economic calendar",
    ),
    # Market Performance Tools - use GenericApiResponse
    ToolMapping(
        "get_sector_performance_snapshot",
        "Market Performance",
        DataSource.GENERIC_API,
        "/sector-performance-snapshot",
        False,
        "Sector performance",
    ),
    ToolMapping(
        "get_biggest_gainers",
        "Market Performance",
        DataSource.GENERIC_API,
        "/biggest-gainers",
        False,
        "Biggest gainers",
    ),
    ToolMapping(
        "get_biggest_losers",
        "Market Performance",
        DataSource.GENERIC_API,
        "/biggest-losers",
        False,
        "Biggest losers",
    ),
    ToolMapping(
        "get_most_actives",
        "Market Performance",
        DataSource.GENERIC_API,
        "/most-actives",
        False,
        "Most active stocks",
    ),
    # Market Hours Tools - use GenericApiResponse
    ToolMapping(
        "get_exchange_market_hours",
        "Market Hours",
        DataSource.GENERIC_API,
        "/exchange-market-hours",
        False,
        "Exchange hours",
    ),
    # Stock Directory Tools - use GenericApiResponse + Company
    ToolMapping(
        "get_all_stock_symbols",
        "Stock Directory",
        DataSource.GENERIC_API,
        "/stock-list",
        False,
        "All stock symbols",
    ),
    ToolMapping(
        "get_all_etfs", "Stock Directory", DataSource.GENERIC_API, "/etf-list", False, "All ETFs"
    ),
    ToolMapping(
        "get_sp500_constituents",
        "Stock Directory",
        DataSource.GENERIC_API,
        "/sp500-constituent",
        False,
        "S&P 500 constituents",
    ),
    # Index Tools - use GenericApiResponse
    ToolMapping(
        "get_available_indexes",
        "Indexes",
        DataSource.GENERIC_API,
        "/index-list",
        False,
        "Available indexes",
    ),
    ToolMapping(
        "get_batch_index_quotes",
        "Indexes",
        DataSource.GENERIC_API,
        "/batch-index-quotes",
        False,
        "Index quotes",
    ),
    # Insider Trading - use GenericApiResponse
    ToolMapping(
        "get_insider_trading_statistics",
        "Insider Trading",
        DataSource.GENERIC_API,
        "/insider-trading/statistics",
        True,
        "Insider trading stats",
    ),
    # Institutional Holdings - use GenericApiResponse
    ToolMapping(
        "get_institutional_holders",
        "Institutional",
        DataSource.GENERIC_API,
        "/institutional-ownership/symbol-positions-summary",
        True,
        "Institutional holders",
    ),
    # SEC Filings - use GenericApiResponse
    ToolMapping(
        "search_sec_filings",
        "SEC Filings",
        DataSource.GENERIC_API,
        "/sec-filings-search/symbol",
        True,
        "SEC filings",
    ),
]


def get_db_path() -> Path:
    """Extract database file path from config URL."""
    config = get_config()
    db_url = config.fmp_database_url

    if ":///" in db_url:
        path_str = db_url.split("///")[-1]
        return Path(path_str)
    return Path("./data/fmp.db")


class ReportBuilder:
    """Builds and formats the database report."""

    def __init__(self, format: str = "text"):
        self.lines: list[str] = []
        self.format = format  # "text" or "markdown"
        self._table_started = False
        self._table_widths: list[int] = []

    def header(self, text: str, char: str = "=") -> None:
        """Add a section header."""
        self._close_table()
        self.lines.append("")
        if self.format == "markdown":
            self.lines.append(f"## {text}")
        else:
            self.lines.append(text)
            self.lines.append(char * len(text))

    def subheader(self, text: str) -> None:
        """Add a subsection header."""
        self._close_table()
        self.lines.append("")
        if self.format == "markdown":
            self.lines.append(f"### {text}")
        else:
            self.lines.append(f"  {text}")
            self.lines.append(f"  {'-' * len(text)}")

    def line(self, text: str = "") -> None:
        """Add a line of text."""
        self._close_table()
        self.lines.append(text)

    def kv(self, key: str, value: str | int | float, indent: int = 2) -> None:
        """Add a key-value pair."""
        self._close_table()
        if self.format == "markdown":
            self.lines.append(f"- **{key}**: {value}")
        else:
            prefix = " " * indent
            self.lines.append(f"{prefix}{key}: {value}")

    def table_row(self, cols: list[str], widths: list[int], indent: int = 2) -> None:
        """Add a table row."""
        if self.format == "markdown":
            # Start markdown table if needed
            if not self._table_started:
                self._table_started = True
                self._table_widths = widths
                self.lines.append("")
                # Header row
                self.lines.append("| " + " | ".join(str(col) for col in cols) + " |")
                # Separator row
                self.lines.append(
                    "| " + " | ".join("-" * max(len(str(col)), 3) for col in cols) + " |"
                )
            else:
                # Data row
                self.lines.append("| " + " | ".join(str(col) for col in cols) + " |")
        else:
            prefix = " " * indent
            formatted = [
                str(col).ljust(w) if i < len(cols) - 1 else str(col).rjust(w)
                for i, (col, w) in enumerate(zip(cols, widths))
            ]
            self.lines.append(prefix + "  ".join(formatted))

    def separator(self, width: int = 60, indent: int = 2) -> None:
        """Add a horizontal separator."""
        if self.format == "markdown":
            pass  # No separator needed in markdown tables
        else:
            self.lines.append(" " * indent + "-" * width)

    def _close_table(self) -> None:
        """Close any open markdown table."""
        if self._table_started:
            self._table_started = False
            self._table_widths = []

    def warning(self, text: str) -> None:
        """Add a warning message."""
        self._close_table()
        if self.format == "markdown":
            self.lines.append(f"> ⚠️ **WARNING**: {text}")
        else:
            self.lines.append(f"  [!] WARNING: {text}")

    def success(self, text: str) -> None:
        """Add a success message."""
        self._close_table()
        if self.format == "markdown":
            self.lines.append(f"> ✅ {text}")
        else:
            self.lines.append(f"  [+] {text}")

    def info(self, text: str) -> None:
        """Add an info message."""
        self._close_table()
        if self.format == "markdown":
            self.lines.append(f"> ℹ️ {text}")
        else:
            self.lines.append(f"  [i] {text}")

    def output(self) -> str:
        """Get the full report as a string."""
        self._close_table()
        return "\n".join(self.lines)


async def get_table_counts() -> dict[str, int]:
    """Get row counts for all tables."""
    counts = {}
    async with get_session() as session:
        for model in ALL_MODELS:
            result = await session.execute(select(func.count()).select_from(model))
            counts[model.__tablename__] = result.scalar() or 0
    return counts


async def get_symbols_in_db() -> set[str]:
    """Get all unique symbols in the database."""
    symbols = set()
    async with get_session() as session:
        # From companies
        result = await session.execute(select(distinct(Company.symbol)))
        symbols.update(row[0] for row in result.fetchall() if row[0])

        # From stock quotes
        result = await session.execute(select(distinct(StockQuote.symbol)))
        symbols.update(row[0] for row in result.fetchall() if row[0])

    return symbols


async def get_cached_endpoints() -> set[str]:
    """Get all cached endpoints from generic_api_responses."""
    async with get_session() as session:
        result = await session.execute(select(distinct(GenericApiResponse.endpoint)))
        return {row[0] for row in result.fetchall() if row[0]}


async def get_endpoint_symbol_count(endpoint: str) -> int:
    """Get count of symbols with data for an endpoint."""
    async with get_session() as session:
        result = await session.execute(
            select(func.count(distinct(GenericApiResponse.symbol))).where(
                GenericApiResponse.endpoint == endpoint
            )
        )
        return result.scalar() or 0


async def get_data_source_status() -> dict[str, dict]:
    """Get status of each data source."""
    counts = await get_table_counts()
    status = {}

    for source in DataSource:
        table_name = source.value
        count = counts.get(table_name, 0)
        status[source.value] = {
            "count": count,
            "has_data": count > 0,
        }

    return status


async def generate_tools_report(report: ReportBuilder) -> None:
    """Generate a report showing all tools and their data sources."""
    report.header("FMP Tools to Data Source Mapping")
    report.kv("Generated at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), indent=0)
    report.line()

    # Group tools by category
    tool_mappings = get_tool_mappings()
    tools_by_category: dict[str, list[ToolMapping]] = defaultdict(list)
    for tool in tool_mappings:
        tools_by_category[tool.category].append(tool)

    report.header("Tools by Category", "-")
    report.line()

    for category in sorted(tools_by_category.keys()):
        tools = tools_by_category[category]
        report.subheader(f"{category} ({len(tools)} tools)")

        widths = [35, 25, 30]
        report.table_row(["Tool", "Data Source", "Endpoint/Note"], widths)
        report.separator(92)

        for tool in sorted(tools, key=lambda t: t.tool_name):
            endpoint_display = tool.endpoint or "(direct table)"
            report.table_row([tool.tool_name, tool.data_source.value, endpoint_display], widths)
        report.line()

    # Summary by data source
    report.header("Summary by Data Source", "-")

    source_counts: dict[DataSource, int] = defaultdict(int)
    for tool in tool_mappings:
        source_counts[tool.data_source] += 1

    widths = [30, 15]
    report.table_row(["Data Source", "Tool Count"], widths)
    report.separator(47)
    for source in DataSource:
        report.table_row([source.value, str(source_counts[source])], widths)
    report.separator(47)
    report.table_row(["TOTAL", str(len(tool_mappings))], widths)


async def generate_availability_report(report: ReportBuilder) -> None:
    """Generate a report showing tool availability with data counts and time ranges."""
    report.header("FMP Tool Availability Report")
    report.kv("Generated at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), indent=0)
    report.line()

    db_path = get_db_path()
    if not db_path.exists():
        report.warning("Database not found!")
        report.info("Run 'make db-use-bundled' to load fixture data")
        return

    # Get data source status, time ranges, and cached endpoints
    source_status = await get_data_source_status()
    time_ranges = await get_data_time_ranges()
    endpoint_coverage = await get_endpoint_coverage()
    cached_endpoints = set(endpoint_coverage.keys())

    # Show data time ranges first
    report.header("Data Time Ranges", "-")
    widths = [25, 18, 15, 15, 12]
    report.table_row(["Data Source", "Date Type", "From", "To", "Records"], widths)
    report.separator(87)

    for source_name, range_info in time_ranges.items():
        if range_info["count"] > 0:
            report.table_row(
                [
                    source_name,
                    range_info.get("date_label", "N/A"),
                    range_info["min_date"] or "N/A",
                    range_info["max_date"] or "N/A",
                    f"{range_info['count']:,}",
                ],
                widths,
            )

    # Get all tools and classify them
    tool_mappings = get_tool_mappings()

    tools_with_data = []
    tools_without_data = []

    for tool in tool_mappings:
        tool_info = {
            "tool": tool,
            "status": "MISSING",
            "count_info": "-",
            "time_range": None,
        }

        if tool.data_source == DataSource.GENERIC_API:
            if tool.endpoint and tool.endpoint in cached_endpoints:
                ep_info = endpoint_coverage[tool.endpoint]
                tool_info["status"] = "OK"
                if ep_info["symbol_count"] > 0:
                    tool_info["count_info"] = (
                        f"{ep_info['response_count']} ({ep_info['symbol_count']} sym)"
                    )
                else:
                    tool_info["count_info"] = f"{ep_info['response_count']} responses"
                # Get time range from generic_api_responses
                tr = time_ranges.get("generic_api_responses", {})
                if tr.get("min_date") and tr.get("max_date"):
                    tool_info["time_range"] = f"{tr['min_date']} to {tr['max_date']}"
                tools_with_data.append(tool_info)
            else:
                tool_info["count_info"] = f"endpoint {tool.endpoint or 'unknown'} not cached"
                tools_without_data.append(tool_info)
        else:
            # Direct table access
            table_status = source_status[tool.data_source.value]
            if table_status["has_data"]:
                tool_info["status"] = "OK"
                tool_info["count_info"] = f"{table_status['count']:,} records"
                # Get time range for this data source
                tr = time_ranges.get(tool.data_source.value, {})
                if tr.get("min_date") and tr.get("max_date"):
                    tool_info["time_range"] = f"{tr['min_date']} to {tr['max_date']}"
                tools_with_data.append(tool_info)
            else:
                tool_info["status"] = "EMPTY"
                tool_info["count_info"] = "table empty"
                tools_without_data.append(tool_info)

    # Report tools WITH data, grouped by category
    report.header("Tools WITH Data", "-")
    report.line()

    # Group tools with data by category
    tools_with_data_by_category: dict[str, list] = defaultdict(list)
    for tool_info in tools_with_data:
        tools_with_data_by_category[tool_info["tool"].category].append(tool_info)

    for category in sorted(tools_with_data_by_category.keys()):
        cat_tools = tools_with_data_by_category[category]
        report.subheader(f"{category} ({len(cat_tools)} tools)")

        widths = [35, 20, 25]
        report.table_row(["Tool", "Records", "Time Range"], widths)
        report.separator(82)

        for tool_info in sorted(cat_tools, key=lambda t: t["tool"].tool_name):
            report.table_row(
                [
                    tool_info["tool"].tool_name,
                    tool_info["count_info"],
                    tool_info["time_range"] or "-",
                ],
                widths,
            )

        report.line()

    # Report tools WITHOUT data
    report.header(f"Tools WITHOUT Data ({len(tools_without_data)} tools)", "-")
    report.line()

    if tools_without_data:
        # Group by category for organization
        tools_without_by_category: dict[str, list] = defaultdict(list)
        for tool_info in tools_without_data:
            tools_without_by_category[tool_info["tool"].category].append(tool_info)

        for category in sorted(tools_without_by_category.keys()):
            cat_tools = tools_without_by_category[category]
            report.subheader(f"{category} ({len(cat_tools)} missing)")

            # Show as a compact table
            widths = [35, 35]
            report.table_row(["Tool", "Reason"], widths)
            report.separator(72)

            for tool_info in sorted(cat_tools, key=lambda x: x["tool"].tool_name):
                reason = tool_info["count_info"]
                # Shorten reason if too long
                if len(reason) > 35:
                    reason = reason[:32] + "..."
                report.table_row([tool_info["tool"].tool_name, reason], widths)

            report.line()
    else:
        report.success("All tools have data available!")

    # Summary
    report.header("Summary", "-")
    total_tools = len(tool_mappings)
    total_available = len(tools_with_data)
    coverage_pct = (total_available / total_tools * 100) if total_tools > 0 else 0
    report.kv("Total tools", total_tools)
    report.kv("Tools with data", total_available)
    report.kv("Tools missing data", len(tools_without_data))
    report.kv("Coverage", f"{coverage_pct:.1f}%")


async def generate_coverage_report(report: ReportBuilder) -> None:
    """Generate a report showing tool data coverage."""
    report.header("FMP Tool Data Coverage Report")
    report.kv("Generated at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), indent=0)
    report.line()

    db_path = get_db_path()
    if not db_path.exists():
        report.warning("Database not found!")
        report.info("Run 'make db-use-bundled' to load fixture data")
        return

    # Get data source status
    source_status = await get_data_source_status()
    cached_endpoints = await get_cached_endpoints()
    symbols_in_db = await get_symbols_in_db()

    # Overall status
    report.header("Data Source Status", "-")
    widths = [30, 12, 15]
    report.table_row(["Data Source", "Records", "Status"], widths)
    report.separator(59)

    for source in DataSource:
        status = source_status[source.value]
        status_str = "OK" if status["has_data"] else "EMPTY"
        report.table_row([source.value, f"{status['count']:,}", status_str], widths)

    # Group tools by category and analyze coverage
    report.header("Tool Coverage by Category", "-")

    tools_by_category: dict[str, list[ToolMapping]] = defaultdict(list)
    for tool in get_tool_mappings():
        tools_by_category[tool.category].append(tool)

    for category in sorted(tools_by_category.keys()):
        tools = tools_by_category[category]

        available_count = 0
        unavailable_tools = []

        for tool in tools:
            has_data = False

            # Check if data source has data
            if tool.data_source == DataSource.GENERIC_API:
                # Check if endpoint is cached
                if tool.endpoint and tool.endpoint in cached_endpoints:
                    has_data = True
            else:
                # Check if table has data
                has_data = source_status[tool.data_source.value]["has_data"]

            if has_data:
                available_count += 1
            else:
                unavailable_tools.append(tool)

        status_icon = (
            "[+]" if available_count == len(tools) else "[!]" if available_count > 0 else "[-]"
        )
        report.line()
        report.line(f"  {status_icon} {category}: {available_count}/{len(tools)} tools have data")

        if unavailable_tools:
            for tool in unavailable_tools[:5]:
                reason = (
                    f"endpoint {tool.endpoint} not cached"
                    if tool.endpoint
                    else f"{tool.data_source.value} table empty"
                )
                report.line(f"      - {tool.tool_name}: {reason}")
            if len(unavailable_tools) > 5:
                report.line(f"      ... and {len(unavailable_tools) - 5} more")

    # Symbol coverage for symbol-requiring tools
    report.header("Symbol Coverage", "-")
    report.kv("Symbols in database", len(symbols_in_db))
    report.kv("Expected symbols (DEFAULT_SYMBOLS)", len(DEFAULT_SYMBOLS))

    missing = set(DEFAULT_SYMBOLS) - symbols_in_db
    if missing:
        report.warning(f"Missing expected symbols: {', '.join(sorted(missing)[:10])}")
    else:
        report.success("All expected symbols present")

    # Summary statistics
    report.header("Coverage Summary", "-")

    tool_mappings = get_tool_mappings()
    total_tools = len(tool_mappings)
    tools_with_data = sum(
        1
        for tool in tool_mappings
        if (
            tool.data_source == DataSource.GENERIC_API
            and tool.endpoint
            and tool.endpoint in cached_endpoints
        )
        or (
            tool.data_source != DataSource.GENERIC_API
            and source_status[tool.data_source.value]["has_data"]
        )
    )

    coverage_pct = (tools_with_data / total_tools * 100) if total_tools > 0 else 0

    report.kv("Total tools mapped", total_tools)
    report.kv("Tools with available data", tools_with_data)
    report.kv("Coverage percentage", f"{coverage_pct:.1f}%")

    if coverage_pct < 50:
        report.line()
        report.warning("Low data coverage! Many tools may return no data.")
        report.info("Run 'make db-download-fixtures' with FMP_API_KEY to download more data")


async def get_symbol_data_coverage(symbol: str) -> dict[str, dict]:
    """Get data coverage for a specific symbol."""
    coverage = {}

    async with get_session() as session:
        # Company profile
        result = await session.execute(select(Company).where(Company.symbol == symbol))
        company = result.scalar_one_or_none()
        coverage["company_profile"] = {
            "has_data": company is not None,
            "count": 1 if company else 0,
        }

        # Stock quote
        result = await session.execute(select(StockQuote).where(StockQuote.symbol == symbol))
        quote = result.scalar_one_or_none()
        coverage["stock_quote"] = {
            "has_data": quote is not None,
            "count": 1 if quote else 0,
            "last_updated": quote.updated_at.isoformat() if quote else None,
        }

        # Historical prices
        result = await session.execute(
            select(
                func.count(),
                func.min(HistoricalPrice.date),
                func.max(HistoricalPrice.date),
            ).where(HistoricalPrice.symbol == symbol)
        )
        row = result.fetchone()
        coverage["historical_prices"] = {
            "has_data": row[0] > 0,
            "count": row[0],
            "date_range": f"{row[1]} to {row[2]}" if row[0] > 0 else None,
        }

        # Financial statements by type
        result = await session.execute(
            select(
                FinancialStatement.statement_type,
                func.count(),
            )
            .where(FinancialStatement.symbol == symbol)
            .group_by(FinancialStatement.statement_type)
        )
        stmt_counts = {row[0]: row[1] for row in result.fetchall()}
        coverage["financial_statements"] = {
            "has_data": len(stmt_counts) > 0,
            "count": sum(stmt_counts.values()),
            "by_type": stmt_counts,
        }

        # Technical indicators by type
        result = await session.execute(
            select(
                TechnicalIndicator.indicator_type,
                func.count(),
            )
            .where(TechnicalIndicator.symbol == symbol)
            .group_by(TechnicalIndicator.indicator_type)
        )
        indicator_counts = {row[0]: row[1] for row in result.fetchall()}
        coverage["technical_indicators"] = {
            "has_data": len(indicator_counts) > 0,
            "count": sum(indicator_counts.values()),
            "by_type": indicator_counts,
        }

        # Generic API responses for this symbol
        result = await session.execute(
            select(
                GenericApiResponse.endpoint,
                func.count(),
            )
            .where(GenericApiResponse.symbol == symbol)
            .group_by(GenericApiResponse.endpoint)
        )
        endpoint_counts = {row[0]: row[1] for row in result.fetchall()}
        coverage["generic_api_responses"] = {
            "has_data": len(endpoint_counts) > 0,
            "count": sum(endpoint_counts.values()),
            "endpoints": endpoint_counts,
        }

    return coverage


async def get_endpoint_coverage() -> dict[str, dict]:
    """Get data coverage by API endpoint."""
    coverage = {}

    async with get_session() as session:
        # Get all endpoints and their counts
        result = await session.execute(
            select(
                GenericApiResponse.endpoint,
                func.count(),
                func.count(distinct(GenericApiResponse.symbol)),
                func.min(GenericApiResponse.created_at),
                func.max(GenericApiResponse.updated_at),
            ).group_by(GenericApiResponse.endpoint)
        )

        for row in result.fetchall():
            coverage[row[0]] = {
                "response_count": row[1],
                "symbol_count": row[2],
                "first_cached": row[3].isoformat() if row[3] else None,
                "last_updated": row[4].isoformat() if row[4] else None,
            }

    return coverage


async def get_data_time_ranges() -> dict[str, dict]:
    """Get time ranges for each data source.

    Returns date_label to describe what the dates represent:
    - For historical data: actual data dates (price date, fiscal date)
    - For snapshots: when the data was downloaded from the API
    """
    ranges = {}

    async with get_session() as session:
        # Historical prices - actual trading dates
        result = await session.execute(
            select(
                func.min(HistoricalPrice.date),
                func.max(HistoricalPrice.date),
                func.count(),
            )
        )
        row = result.fetchone()
        ranges["historical_prices"] = {
            "min_date": str(row[0]) if row[0] else None,
            "max_date": str(row[1]) if row[1] else None,
            "count": row[2],
            "date_label": "Price dates",
        }

        # Financial statements - fiscal period dates
        result = await session.execute(
            select(
                func.min(FinancialStatement.fiscal_date),
                func.max(FinancialStatement.fiscal_date),
                func.count(),
            )
        )
        row = result.fetchone()
        # Handle sentinel date (0001-01-01) used for TTM records
        min_date = row[0]
        if min_date and str(min_date) == "0001-01-01":
            result2 = await session.execute(
                select(func.min(FinancialStatement.fiscal_date)).where(
                    FinancialStatement.fiscal_date > min_date
                )
            )
            min_date = result2.scalar()
        ranges["financial_statements"] = {
            "min_date": str(min_date) if min_date else None,
            "max_date": str(row[1]) if row[1] else None,
            "count": row[2],
            "date_label": "Fiscal periods",
        }

        # Technical indicators - indicator calculation dates
        result = await session.execute(
            select(
                func.min(TechnicalIndicator.date),
                func.max(TechnicalIndicator.date),
                func.count(),
            )
        )
        row = result.fetchone()
        ranges["technical_indicators"] = {
            "min_date": str(row[0]) if row[0] else None,
            "max_date": str(row[1]) if row[1] else None,
            "count": row[2],
            "date_label": "Indicator dates",
        }

        # Stock quotes - quote timestamp from API
        result = await session.execute(
            select(
                func.min(StockQuote.quote_timestamp),
                func.max(StockQuote.quote_timestamp),
                func.count(),
            )
        )
        row = result.fetchone()
        ranges["stock_quotes"] = {
            "min_date": row[0].strftime("%Y-%m-%d %H:%M") if row[0] else None,
            "max_date": row[1].strftime("%Y-%m-%d %H:%M") if row[1] else None,
            "count": row[2],
            "date_label": "Quote timestamps",
        }

        # Companies - when profiles were downloaded
        result = await session.execute(
            select(
                func.min(Company.updated_at),
                func.max(Company.updated_at),
                func.count(),
            )
        )
        row = result.fetchone()
        ranges["companies"] = {
            "min_date": row[0].strftime("%Y-%m-%d") if row[0] else None,
            "max_date": row[1].strftime("%Y-%m-%d") if row[1] else None,
            "count": row[2],
            "date_label": "Downloaded on",
        }

        # Generic API responses - when responses were downloaded
        result = await session.execute(
            select(
                func.min(GenericApiResponse.updated_at),
                func.max(GenericApiResponse.updated_at),
                func.count(),
            )
        )
        row = result.fetchone()
        ranges["generic_api_responses"] = {
            "min_date": row[0].strftime("%Y-%m-%d") if row[0] else None,
            "max_date": row[1].strftime("%Y-%m-%d") if row[1] else None,
            "count": row[2],
            "date_label": "Downloaded on",
        }

    return ranges


async def get_data_freshness() -> dict[str, datetime | None]:
    """Get the most recent update timestamp for each table."""
    freshness = {}

    async with get_session() as session:
        # Stock quotes
        result = await session.execute(select(func.max(StockQuote.updated_at)))
        freshness["stock_quotes"] = result.scalar()

        # Historical prices
        result = await session.execute(select(func.max(HistoricalPrice.updated_at)))
        freshness["historical_prices"] = result.scalar()

        # Financial statements
        result = await session.execute(select(func.max(FinancialStatement.updated_at)))
        freshness["financial_statements"] = result.scalar()

        # Technical indicators
        result = await session.execute(select(func.max(TechnicalIndicator.updated_at)))
        freshness["technical_indicators"] = result.scalar()

        # Generic responses
        result = await session.execute(select(func.max(GenericApiResponse.updated_at)))
        freshness["generic_api_responses"] = result.scalar()

    return freshness


async def find_missing_data() -> dict[str, list[str]]:
    """Find expected data that is missing."""
    missing = defaultdict(list)

    symbols_in_db = await get_symbols_in_db()
    expected_symbols = set(DEFAULT_SYMBOLS)

    # Check for missing expected symbols
    missing_symbols = expected_symbols - symbols_in_db
    if missing_symbols:
        missing["expected_symbols_not_in_db"] = sorted(missing_symbols)

    async with get_session() as session:
        # For each symbol in DB, check for missing data types
        for symbol in sorted(symbols_in_db)[:25]:  # Limit to first 25 for performance
            # Check company profile
            result = await session.execute(
                select(func.count()).select_from(Company).where(Company.symbol == symbol)
            )
            if result.scalar() == 0:
                missing["symbols_missing_company_profile"].append(symbol)

            # Check stock quote
            result = await session.execute(
                select(func.count()).select_from(StockQuote).where(StockQuote.symbol == symbol)
            )
            if result.scalar() == 0:
                missing["symbols_missing_stock_quote"].append(symbol)

            # Check historical prices
            result = await session.execute(
                select(func.count())
                .select_from(HistoricalPrice)
                .where(HistoricalPrice.symbol == symbol)
            )
            if result.scalar() == 0:
                missing["symbols_missing_historical_prices"].append(symbol)

            # Check financial statements
            result = await session.execute(
                select(func.count())
                .select_from(FinancialStatement)
                .where(FinancialStatement.symbol == symbol)
            )
            if result.scalar() == 0:
                missing["symbols_missing_financial_statements"].append(symbol)

    return dict(missing)


async def get_endpoint_details(endpoint: str) -> dict:
    """Get detailed information about a specific endpoint."""
    details = {
        "endpoint": endpoint,
        "responses": [],
        "symbols": [],
    }

    async with get_session() as session:
        # Get all responses for this endpoint
        result = await session.execute(
            select(GenericApiResponse)
            .where(GenericApiResponse.endpoint == endpoint)
            .order_by(GenericApiResponse.updated_at.desc())
            .limit(50)
        )

        for response in result.scalars():
            response_info = {
                "symbol": response.symbol,
                "params_hash": response.params_hash[:8] + "...",
                "http_status": response.http_status,
                "updated_at": response.updated_at.isoformat(),
            }

            # Try to parse response data to check if it's empty
            try:
                data = json.loads(response.response_data)
                if isinstance(data, list):
                    response_info["data_count"] = len(data)
                    response_info["is_empty"] = len(data) == 0
                elif isinstance(data, dict):
                    response_info["data_keys"] = list(data.keys())[:5]
                    response_info["is_empty"] = len(data) == 0
                else:
                    response_info["data_type"] = type(data).__name__
            except json.JSONDecodeError:
                response_info["data_error"] = "Invalid JSON"

            details["responses"].append(response_info)
            if response.symbol:
                details["symbols"].append(response.symbol)

        details["symbols"] = sorted(set(details["symbols"]))

    return details


async def generate_summary_report(report: ReportBuilder) -> None:
    """Generate a summary report."""
    db_path = get_db_path()

    report.header("FMP Database Summary Report")
    report.kv("Generated at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), indent=0)
    report.line()

    # Database file info
    if db_path.exists():
        file_size = db_path.stat().st_size
        modified_time = datetime.fromtimestamp(db_path.stat().st_mtime)
        report.kv("Database path", str(db_path), indent=0)
        report.kv("File size", f"{file_size / 1024:.1f} KB ({file_size:,} bytes)", indent=0)
        report.kv("Last modified", modified_time.strftime("%Y-%m-%d %H:%M:%S"), indent=0)
    else:
        report.warning(f"Database not found at {db_path}")
        report.info("Run 'make db-use-bundled' or 'make db-download-fixtures' to populate data")
        return

    # Table counts
    report.header("Table Statistics", "-")
    counts = await get_table_counts()
    total = sum(counts.values())

    widths = [30, 12]
    report.table_row(["Table", "Rows"], widths)
    report.separator(44)
    for table, count in counts.items():
        report.table_row([table, f"{count:,}"], widths)
    report.separator(44)
    report.table_row(["TOTAL", f"{total:,}"], widths)

    if total == 0:
        report.line()
        report.warning("Database is empty! No data available for tools.")
        report.info("Run 'make db-use-bundled' to load bundled fixture data")
        return

    # Symbols overview
    report.header("Symbol Coverage", "-")
    symbols = await get_symbols_in_db()
    report.kv("Total unique symbols", len(symbols))

    expected_coverage = len(symbols & set(DEFAULT_SYMBOLS))
    report.kv("Expected symbols covered", f"{expected_coverage}/{len(DEFAULT_SYMBOLS)}")

    if symbols:
        sample = sorted(symbols)[:10]
        report.kv("Sample symbols", ", ".join(sample))
        if len(symbols) > 10:
            report.info(f"(showing 10 of {len(symbols)} symbols)")

    # Data freshness
    report.header("Data Freshness", "-")
    freshness = await get_data_freshness()
    for table, updated_at in freshness.items():
        if updated_at:
            report.kv(table, updated_at.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            report.kv(table, "No data")


async def generate_full_report(report: ReportBuilder) -> None:
    """Generate a full detailed report."""
    await generate_summary_report(report)

    db_path = get_db_path()
    if not db_path.exists():
        return

    counts = await get_table_counts()
    if sum(counts.values()) == 0:
        return

    # Missing data analysis
    report.header("Missing Data Analysis", "-")
    missing = await find_missing_data()

    if not missing:
        report.success("No critical missing data detected")
    else:
        for category, items in missing.items():
            report.subheader(category.replace("_", " ").title())
            for item in items[:10]:  # Limit display
                report.line(f"    - {item}")
            if len(items) > 10:
                report.info(f"    ... and {len(items) - 10} more")

    # Endpoint coverage
    report.header("API Endpoint Coverage", "-")
    endpoint_coverage = await get_endpoint_coverage()

    if endpoint_coverage:
        # Group by category
        symbol_endpoints = {k: v for k, v in endpoint_coverage.items() if v["symbol_count"] > 0}
        global_endpoints = {k: v for k, v in endpoint_coverage.items() if v["symbol_count"] == 0}

        if symbol_endpoints:
            report.subheader("Symbol-Specific Endpoints")
            widths = [40, 10, 10]
            report.table_row(["Endpoint", "Responses", "Symbols"], widths)
            report.separator(62)
            for endpoint in sorted(symbol_endpoints.keys()):
                info = symbol_endpoints[endpoint]
                report.table_row(
                    [endpoint[:40], str(info["response_count"]), str(info["symbol_count"])], widths
                )

        if global_endpoints:
            report.subheader("Global Endpoints (no symbol)")
            widths = [40, 10]
            report.table_row(["Endpoint", "Responses"], widths)
            report.separator(52)
            for endpoint in sorted(global_endpoints.keys()):
                info = global_endpoints[endpoint]
                report.table_row([endpoint[:40], str(info["response_count"])], widths)

        # Check for expected but missing endpoints
        expected_endpoints = set(ep.path for ep in SYMBOL_ENDPOINTS + NON_SYMBOL_ENDPOINTS)
        cached_endpoints = set(endpoint_coverage.keys())
        missing_endpoints = expected_endpoints - cached_endpoints

        if missing_endpoints:
            report.subheader("Missing Endpoints (not cached)")
            for ep in sorted(missing_endpoints)[:20]:
                report.line(f"    - {ep}")
            if len(missing_endpoints) > 20:
                report.info(f"    ... and {len(missing_endpoints) - 20} more")
    else:
        report.warning("No endpoints cached in generic_api_responses")

    # Financial statement breakdown
    report.header("Financial Statement Breakdown", "-")
    async with get_session() as session:
        result = await session.execute(
            select(
                FinancialStatement.statement_type,
                FinancialStatement.period,
                func.count(),
                func.count(distinct(FinancialStatement.symbol)),
            )
            .group_by(FinancialStatement.statement_type, FinancialStatement.period)
            .order_by(FinancialStatement.statement_type, FinancialStatement.period)
        )

        stmt_data = result.fetchall()
        if stmt_data:
            widths = [25, 10, 10, 10]
            report.table_row(["Statement Type", "Period", "Records", "Symbols"], widths)
            report.separator(58)
            for row in stmt_data:
                report.table_row([row[0], row[1], str(row[2]), str(row[3])], widths)
        else:
            report.warning("No financial statements in database")

    # Technical indicator breakdown
    report.header("Technical Indicator Breakdown", "-")
    async with get_session() as session:
        result = await session.execute(
            select(
                TechnicalIndicator.indicator_type,
                TechnicalIndicator.period,
                func.count(),
                func.count(distinct(TechnicalIndicator.symbol)),
            )
            .group_by(TechnicalIndicator.indicator_type, TechnicalIndicator.period)
            .order_by(TechnicalIndicator.indicator_type, TechnicalIndicator.period)
        )

        indicator_data = result.fetchall()
        if indicator_data:
            widths = [15, 10, 10, 10]
            report.table_row(["Indicator", "Period", "Records", "Symbols"], widths)
            report.separator(48)
            for row in indicator_data:
                report.table_row([row[0], str(row[1]), str(row[2]), str(row[3])], widths)
        else:
            report.warning("No technical indicators in database")


async def generate_symbol_report(report: ReportBuilder, symbol: str) -> None:
    """Generate a report for a specific symbol."""
    report.header(f"Symbol Report: {symbol}")
    report.kv("Generated at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), indent=0)
    report.line()

    coverage = await get_symbol_data_coverage(symbol)

    # Summary
    has_any_data = any(c["has_data"] for c in coverage.values())

    if not has_any_data:
        report.warning(f"No data found for symbol {symbol}")
        report.info("This symbol may not be in the fixture data")
        report.info(
            "Available symbols can be seen with: uv run python -m scripts.db_report --summary"
        )
        return

    # Company profile
    report.header("Company Profile", "-")
    if coverage["company_profile"]["has_data"]:
        report.success("Company profile available")
    else:
        report.warning("No company profile - /profile endpoint may not return data")

    # Stock quote
    report.header("Stock Quote", "-")
    if coverage["stock_quote"]["has_data"]:
        report.success("Stock quote available")
        report.kv("Last updated", coverage["stock_quote"]["last_updated"])
    else:
        report.warning("No stock quote - /quote endpoint may not return data")

    # Historical prices
    report.header("Historical Prices", "-")
    if coverage["historical_prices"]["has_data"]:
        report.success(f"{coverage['historical_prices']['count']:,} price records available")
        report.kv("Date range", coverage["historical_prices"]["date_range"])
    else:
        report.warning("No historical prices - price history tools may not return data")

    # Financial statements
    report.header("Financial Statements", "-")
    if coverage["financial_statements"]["has_data"]:
        report.success(f"{coverage['financial_statements']['count']} statement records available")
        report.subheader("By Type")
        for stmt_type, count in sorted(coverage["financial_statements"]["by_type"].items()):
            report.kv(stmt_type, count, indent=4)
    else:
        report.warning("No financial statements - financial tools may not return data")

    # Technical indicators
    report.header("Technical Indicators", "-")
    if coverage["technical_indicators"]["has_data"]:
        report.success(f"{coverage['technical_indicators']['count']} indicator records available")
        report.subheader("By Type")
        for ind_type, count in sorted(coverage["technical_indicators"]["by_type"].items()):
            report.kv(ind_type, count, indent=4)
    else:
        report.warning("No technical indicators - indicator tools may not return data")

    # Generic API responses - map to tools
    report.header("Cached API Responses (Tool Mapping)", "-")
    if coverage["generic_api_responses"]["has_data"]:
        report.success(f"{coverage['generic_api_responses']['count']} cached responses")
        report.subheader("Endpoints → Tools")

        for endpoint, count in sorted(coverage["generic_api_responses"]["endpoints"].items()):
            # Find matching tools
            matching_tools = [t.tool_name for t in get_tool_mappings() if t.endpoint == endpoint]
            tools_str = ", ".join(matching_tools) if matching_tools else "(no mapped tool)"
            report.line(f"    {endpoint}: {count} → {tools_str}")
    else:
        report.info("No additional cached API responses for this symbol")

    # Show which tools will work for this symbol
    report.header("Tool Availability for This Symbol", "-")

    cached_endpoints = set(coverage["generic_api_responses"]["endpoints"].keys())

    tools_available = []
    tools_unavailable = []

    for tool in get_tool_mappings():
        if not tool.requires_symbol:
            continue

        if tool.data_source == DataSource.STOCK_QUOTE:
            if coverage["stock_quote"]["has_data"]:
                tools_available.append(tool.tool_name)
            else:
                tools_unavailable.append((tool.tool_name, "no stock quote"))
        elif tool.data_source == DataSource.COMPANY:
            if coverage["company_profile"]["has_data"]:
                tools_available.append(tool.tool_name)
            else:
                tools_unavailable.append((tool.tool_name, "no company profile"))
        elif tool.data_source == DataSource.HISTORICAL_PRICE:
            if coverage["historical_prices"]["has_data"]:
                tools_available.append(tool.tool_name)
            else:
                tools_unavailable.append((tool.tool_name, "no historical prices"))
        elif tool.data_source == DataSource.FINANCIAL_STATEMENT:
            if coverage["financial_statements"]["has_data"]:
                tools_available.append(tool.tool_name)
            else:
                tools_unavailable.append((tool.tool_name, "no financial statements"))
        elif tool.data_source == DataSource.TECHNICAL_INDICATOR:
            if coverage["technical_indicators"]["has_data"]:
                tools_available.append(tool.tool_name)
            else:
                tools_unavailable.append((tool.tool_name, "no technical indicators"))
        elif tool.data_source == DataSource.GENERIC_API:
            if tool.endpoint and tool.endpoint in cached_endpoints:
                tools_available.append(tool.tool_name)
            else:
                tools_unavailable.append((tool.tool_name, f"endpoint {tool.endpoint} not cached"))

    report.kv("Tools with data", len(tools_available))
    report.kv("Tools without data", len(tools_unavailable))

    if tools_unavailable:
        report.subheader("Unavailable Tools (will not return data)")
        for tool_name, reason in tools_unavailable[:15]:
            report.line(f"    - {tool_name}: {reason}")
        if len(tools_unavailable) > 15:
            report.info(f"    ... and {len(tools_unavailable) - 15} more")


async def generate_endpoint_report(report: ReportBuilder, endpoint: str) -> None:
    """Generate a report for a specific endpoint."""
    report.header(f"Endpoint Report: {endpoint}")
    report.kv("Generated at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), indent=0)
    report.line()

    # Find tools that use this endpoint
    matching_tools = [t for t in get_tool_mappings() if t.endpoint == endpoint]

    if matching_tools:
        report.header("Tools Using This Endpoint", "-")
        for tool in matching_tools:
            report.line(f"    - {tool.tool_name}: {tool.description}")
    else:
        report.info(f"No mapped tools found for endpoint {endpoint}")

    details = await get_endpoint_details(endpoint)

    if not details["responses"]:
        report.warning(f"No cached responses for endpoint {endpoint}")
        report.info("This endpoint may not have been downloaded")
        report.info("Run 'make db-download-fixtures' to fetch data from FMP API")
        return

    report.header("Cached Data", "-")
    report.kv("Total responses", len(details["responses"]))
    report.kv("Unique symbols", len(details["symbols"]))

    if details["symbols"]:
        report.kv("Symbols", ", ".join(details["symbols"][:20]))
        if len(details["symbols"]) > 20:
            report.info(f"  (showing 20 of {len(details['symbols'])} symbols)")

    # Response details
    report.header("Response Details", "-")

    # Check for empty responses
    empty_count = sum(1 for r in details["responses"] if r.get("is_empty", False))
    if empty_count > 0:
        report.warning(f"{empty_count} responses are empty (API returned no data)")

    # Check for errors
    error_count = sum(1 for r in details["responses"] if r.get("http_status", 200) != 200)
    if error_count > 0:
        report.warning(f"{error_count} responses have non-200 HTTP status")

    # Sample responses
    report.subheader("Sample Responses (most recent)")
    for resp in details["responses"][:10]:
        report.line(f"    Symbol: {resp.get('symbol', 'N/A')}")
        report.line(f"      Status: {resp.get('http_status', 'N/A')}")
        report.line(f"      Updated: {resp.get('updated_at', 'N/A')}")
        if "data_count" in resp:
            report.line(f"      Items: {resp['data_count']}")
        if resp.get("is_empty"):
            report.line("      [EMPTY RESPONSE]")
        report.line()


async def main_async(args: argparse.Namespace) -> str:
    """Async main function."""
    report = ReportBuilder(format=args.format)

    try:
        if args.tools:
            await generate_tools_report(report)
        elif args.availability:
            await generate_availability_report(report)
        elif args.coverage:
            await generate_coverage_report(report)
        elif args.symbol:
            await generate_symbol_report(report, args.symbol.upper())
        elif args.endpoint:
            await generate_endpoint_report(report, args.endpoint)
        elif args.summary:
            await generate_summary_report(report)
        else:
            await generate_full_report(report)
    finally:
        await close_engine()

    return report.output()


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Generate detailed database diagnostic report for FMP MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python -m scripts.db_report                    # Full report
  uv run python -m scripts.db_report --summary          # Summary only
  uv run python -m scripts.db_report --tools            # Show all tools and data sources
  uv run python -m scripts.db_report --availability     # Show tool availability with counts
  uv run python -m scripts.db_report --coverage         # Show detailed tool data coverage
  uv run python -m scripts.db_report --symbol AAPL      # Check specific symbol
  uv run python -m scripts.db_report --endpoint /quote  # Check specific endpoint
  uv run python -m scripts.db_report -f markdown        # Output in markdown format
  uv run python -m scripts.db_report --availability -f md -o report.md  # Markdown file
        """,
    )

    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show summary report only",
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="Show all tools and their data source mappings",
    )
    parser.add_argument(
        "--availability",
        action="store_true",
        help="Show tool availability with data counts (quick reference)",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Show tool data coverage report (detailed analysis)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        help="Generate report for a specific symbol",
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        help="Generate report for a specific API endpoint",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Write report to file instead of stdout",
    )
    parser.add_argument(
        "--format",
        "-f",
        type=str,
        choices=["text", "markdown", "md"],
        default="text",
        help="Output format: text (default) or markdown",
    )

    args = parser.parse_args()

    # Normalize format
    if args.format == "md":
        args.format = "markdown"

    # Run the async main
    output = asyncio.run(main_async(args))

    # Output result
    if args.output:
        Path(args.output).write_text(output)
        print(f"Report written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
