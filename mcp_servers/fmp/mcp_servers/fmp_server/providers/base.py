"""Abstract base provider for FMP MCP Server.

This module provides:
- BaseProvider abstract class with 173 method signatures
- ResponseMeta TypedDict for type-safe metadata
- _add_metadata() helper for injecting _meta field into responses

Provider implementations (OnlineProvider, OfflineProvider) must implement
all abstract methods defined here.
"""

import sys
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

# Support both module and direct execution
try:
    from ..config import Mode
except ImportError:
    # Direct execution - add parent to path for local imports
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import Mode


class ResponseMeta(TypedDict):
    """Metadata added to all provider responses."""

    mode: str  # "online" or "offline"
    provider: str  # "fmp-api" or "fmp-fixtures"
    timestamp: str  # ISO 8601 timestamp


class BaseProvider(ABC):
    """Abstract base provider for FMP data operations.

    All provider implementations must inherit from this class and implement
    all abstract methods. The provider pattern abstracts the data source
    (live API vs offline fixtures) from the tool layer.

    Attributes:
        mode: The operation mode (ONLINE or OFFLINE)
        provider_name: Human-readable provider identifier
    """

    def __init__(self, mode: Mode, provider_name: str) -> None:
        """Initialize the provider.

        Args:
            mode: The operation mode (Mode.ONLINE or Mode.OFFLINE)
            provider_name: Identifier for the provider (e.g., "fmp-api", "fmp-fixtures")
        """
        self._mode = mode
        self._provider_name = provider_name

    @property
    def mode(self) -> Mode:
        """Get the current operation mode."""
        return self._mode

    @property
    def provider_name(self) -> str:
        """Get the provider name."""
        return self._provider_name

    def _add_metadata(self, response: dict[str, Any]) -> dict[str, Any]:
        """Add _meta field to response for mode/provider tracking.

        This method injects metadata into every response to indicate:
        - The current mode (online/offline)
        - The provider that served the request
        - The timestamp when the response was generated

        Args:
            response: The response dictionary to augment

        Returns:
            The response with _meta field added
        """
        meta: ResponseMeta = {
            "mode": self._mode.value,
            "provider": self._provider_name,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return {**response, "_meta": meta}

    # =========================================================================
    # Stock Quotes (quote.py) - 18 methods
    # =========================================================================

    @abstractmethod
    async def get_stock_quote(self, symbol: str) -> dict[str, Any]:
        """Get real-time stock quote for a symbol."""
        ...

    @abstractmethod
    async def get_stock_quote_short(self, symbol: str) -> dict[str, Any]:
        """Get condensed real-time stock quote."""
        ...

    @abstractmethod
    async def get_aftermarket_trade(self, symbol: str) -> dict[str, Any]:
        """Get aftermarket trade data for a symbol."""
        ...

    @abstractmethod
    async def get_aftermarket_quote(self, symbol: str) -> dict[str, Any]:
        """Get aftermarket quote data for a symbol."""
        ...

    @abstractmethod
    async def get_stock_price_change(self, symbol: str) -> dict[str, Any]:
        """Get stock price change over multiple periods."""
        ...

    @abstractmethod
    async def get_batch_stock_quotes(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for multiple stocks."""
        ...

    @abstractmethod
    async def get_batch_stock_quotes_short(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get condensed quotes for multiple stocks."""
        ...

    @abstractmethod
    async def get_batch_aftermarket_trades(self, symbols: str) -> dict[str, Any]:
        """Get aftermarket trades for multiple stocks."""
        ...

    @abstractmethod
    async def get_batch_aftermarket_quotes(
        self, symbols: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get aftermarket quotes for multiple stocks."""
        ...

    @abstractmethod
    async def get_exchange_stock_quotes(
        self, exchange: str, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all stocks on an exchange."""
        ...

    @abstractmethod
    async def get_all_mutualfund_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all mutual funds."""
        ...

    @abstractmethod
    async def get_all_etf_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all ETFs."""
        ...

    @abstractmethod
    async def get_all_commodity_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all commodities."""
        ...

    @abstractmethod
    async def get_all_crypto_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all cryptocurrencies."""
        ...

    @abstractmethod
    async def get_all_forex_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all forex pairs."""
        ...

    @abstractmethod
    async def get_all_index_quotes(
        self, short: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get quotes for all market indexes."""
        ...

    # =========================================================================
    # Company Search (company_search.py) - 6 methods
    # =========================================================================

    @abstractmethod
    async def search_by_symbol(
        self, query: str, limit: int | None = None, exchange: str | None = None
    ) -> dict[str, Any]:
        """Search companies by ticker symbol."""
        ...

    @abstractmethod
    async def search_by_company_name(
        self, query: str, limit: int | None = None, exchange: str | None = None
    ) -> dict[str, Any]:
        """Search companies by name."""
        ...

    @abstractmethod
    async def search_by_cik(self, cik: str) -> dict[str, Any]:
        """Search companies by CIK number."""
        ...

    @abstractmethod
    async def search_by_cusip(self, cusip: str) -> dict[str, Any]:
        """Search companies by CUSIP identifier."""
        ...

    @abstractmethod
    async def search_by_isin(self, isin: str) -> dict[str, Any]:
        """Search companies by ISIN identifier."""
        ...

    @abstractmethod
    async def screen_stocks(
        self,
        market_cap_more_than: int | None = None,
        market_cap_lower_than: int | None = None,
        price_more_than: float | None = None,
        price_lower_than: float | None = None,
        beta_more_than: float | None = None,
        beta_lower_than: float | None = None,
        volume_more_than: int | None = None,
        volume_lower_than: int | None = None,
        dividend_more_than: float | None = None,
        dividend_lower_than: float | None = None,
        is_etf: bool | None = None,
        is_fund: bool | None = None,
        is_actively_trading: bool | None = None,
        sector: str | None = None,
        industry: str | None = None,
        country: str | None = None,
        exchange: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Screen stocks based on various criteria."""
        ...

    # =========================================================================
    # Company Information (company_information.py) - 24 methods
    # =========================================================================

    @abstractmethod
    async def get_company_profile(self, symbol: str) -> dict[str, Any]:
        """Get comprehensive company profile."""
        ...

    @abstractmethod
    async def get_company_notes(self, symbol: str) -> dict[str, Any]:
        """Get company notes and filings."""
        ...

    @abstractmethod
    async def get_company_executives(
        self, symbol: str, active: bool | None = None
    ) -> dict[str, Any]:
        """Get company executive information."""
        ...

    @abstractmethod
    async def get_executive_compensation(self, symbol: str) -> dict[str, Any]:
        """Get executive compensation data."""
        ...

    @abstractmethod
    async def get_executive_comp_benchmark(self, year: int | None = None) -> dict[str, Any]:
        """Get executive compensation benchmarks."""
        ...

    @abstractmethod
    async def get_company_dividends(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company dividend history."""
        ...

    @abstractmethod
    async def get_company_earnings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company earnings data."""
        ...

    @abstractmethod
    async def get_stock_splits(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get stock split history."""
        ...

    @abstractmethod
    async def get_stock_peers(self, symbol: str) -> dict[str, Any]:
        """Get peer companies for a stock."""
        ...

    @abstractmethod
    async def get_market_cap(self, symbol: str) -> dict[str, Any]:
        """Get current market capitalization."""
        ...

    @abstractmethod
    async def get_historical_market_cap(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical market capitalization."""
        ...

    @abstractmethod
    async def get_batch_market_cap(self, symbols: str) -> dict[str, Any]:
        """Get market cap for multiple symbols."""
        ...

    @abstractmethod
    async def get_shares_float(self, symbol: str) -> dict[str, Any]:
        """Get shares float data."""
        ...

    @abstractmethod
    async def get_all_shares_float(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get shares float for all companies."""
        ...

    @abstractmethod
    async def get_employee_count(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get company employee count."""
        ...

    @abstractmethod
    async def get_historical_employee_count(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get historical employee count."""
        ...

    @abstractmethod
    async def get_stock_grades(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get analyst stock grades."""
        ...

    @abstractmethod
    async def get_revenue_by_geography(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get revenue breakdown by geography."""
        ...

    @abstractmethod
    async def get_revenue_by_product(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get revenue breakdown by product."""
        ...

    @abstractmethod
    async def get_profile_by_cik(self, cik: str) -> dict[str, Any]:
        """Get company profile by CIK."""
        ...

    @abstractmethod
    async def get_enterprise_values(
        self, symbol: str, period: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get enterprise value data."""
        ...

    @abstractmethod
    async def get_ratings_snapshot(self, symbol: str) -> dict[str, Any]:
        """Get current analyst ratings snapshot."""
        ...

    @abstractmethod
    async def get_ratings_historical(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get historical analyst ratings."""
        ...

    @abstractmethod
    async def get_owner_earnings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get owner earnings data."""
        ...

    # =========================================================================
    # Financial Statements (financial_statements.py) - 27 methods
    # =========================================================================

    @abstractmethod
    async def get_income_statement(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement data."""
        ...

    @abstractmethod
    async def get_balance_sheet(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet data."""
        ...

    @abstractmethod
    async def get_cash_flow_statement(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow statement data."""
        ...

    @abstractmethod
    async def get_latest_financials(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest financial statements across companies."""
        ...

    @abstractmethod
    async def get_stocks_with_financials(self, page: int = 0) -> dict[str, Any]:
        """Get list of stocks with financial data."""
        ...

    @abstractmethod
    async def get_income_statement_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months income statement."""
        ...

    @abstractmethod
    async def get_balance_sheet_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months balance sheet."""
        ...

    @abstractmethod
    async def get_cash_flow_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months cash flow."""
        ...

    @abstractmethod
    async def get_income_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement as reported."""
        ...

    @abstractmethod
    async def get_balance_sheet_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet as reported."""
        ...

    @abstractmethod
    async def get_cash_flow_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow statement as reported."""
        ...

    @abstractmethod
    async def get_full_financials_as_reported(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get full financial statements as reported."""
        ...

    @abstractmethod
    async def get_financial_report_json(
        self, symbol: str, year: int, period: str = "FY"
    ) -> dict[str, Any]:
        """Get financial report in JSON format."""
        ...

    @abstractmethod
    async def get_financial_report_xlsx(
        self, symbol: str, year: int, period: str = "FY"
    ) -> dict[str, Any]:
        """Get financial report in XLSX format."""
        ...

    @abstractmethod
    async def get_financial_reports_dates(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get available financial report dates."""
        ...

    @abstractmethod
    async def get_key_metrics(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get key financial metrics."""
        ...

    @abstractmethod
    async def get_key_metrics_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months key metrics."""
        ...

    @abstractmethod
    async def get_financial_ratios(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get financial ratios."""
        ...

    @abstractmethod
    async def get_ratios_ttm(self, symbol: str) -> dict[str, Any]:
        """Get trailing twelve months ratios."""
        ...

    @abstractmethod
    async def get_financial_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get financial growth metrics."""
        ...

    @abstractmethod
    async def get_income_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get income statement growth metrics."""
        ...

    @abstractmethod
    async def get_balance_sheet_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get balance sheet growth metrics."""
        ...

    @abstractmethod
    async def get_cash_flow_growth(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get cash flow growth metrics."""
        ...

    @abstractmethod
    async def get_financial_scores(self, symbol: str) -> dict[str, Any]:
        """Get financial health scores."""
        ...

    # =========================================================================
    # Technical Indicators (technical_indicators.py) - 10 methods
    # =========================================================================

    @abstractmethod
    async def get_sma(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Simple Moving Average."""
        ...

    @abstractmethod
    async def get_ema(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Exponential Moving Average."""
        ...

    @abstractmethod
    async def get_wma(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Weighted Moving Average."""
        ...

    @abstractmethod
    async def get_dema(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Double Exponential Moving Average."""
        ...

    @abstractmethod
    async def get_tema(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Triple Exponential Moving Average."""
        ...

    @abstractmethod
    async def get_williams(
        self,
        symbol: str,
        period: int = 14,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Williams %R indicator."""
        ...

    @abstractmethod
    async def get_rsi(
        self,
        symbol: str,
        period: int = 14,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Relative Strength Index."""
        ...

    @abstractmethod
    async def get_adx(
        self,
        symbol: str,
        period: int = 14,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Average Directional Index."""
        ...

    @abstractmethod
    async def get_standard_deviation(
        self,
        symbol: str,
        period: int = 20,
        interval: str = "1day",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get Standard Deviation indicator."""
        ...

    # =========================================================================
    # Charts / Historical Prices (charts.py) - 11 methods
    # =========================================================================

    @abstractmethod
    async def get_historical_price_full(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get full historical price data."""
        ...

    @abstractmethod
    async def get_historical_price_light(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get lightweight historical price data."""
        ...

    @abstractmethod
    async def get_historical_price_dividend_adjusted(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get dividend-adjusted historical prices."""
        ...

    @abstractmethod
    async def get_historical_price_unadjusted(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get unadjusted historical prices."""
        ...

    @abstractmethod
    async def get_intraday_1min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 1-minute intraday data."""
        ...

    @abstractmethod
    async def get_intraday_5min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 5-minute intraday data."""
        ...

    @abstractmethod
    async def get_intraday_15min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 15-minute intraday data."""
        ...

    @abstractmethod
    async def get_intraday_30min(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 30-minute intraday data."""
        ...

    @abstractmethod
    async def get_intraday_1hour(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 1-hour intraday data."""
        ...

    @abstractmethod
    async def get_intraday_4hour(
        self,
        symbol: str,
        from_date: str | None = None,
        to_date: str | None = None,
        nonadjusted: bool | None = None,
    ) -> dict[str, Any]:
        """Get 4-hour intraday data."""
        ...

    # =========================================================================
    # Analyst (analyst.py) - 12 methods
    # =========================================================================

    @abstractmethod
    async def get_analyst_estimates(
        self, symbol: str, period: str = "annual", limit: int | None = None
    ) -> dict[str, Any]:
        """Get analyst estimates."""
        ...

    @abstractmethod
    async def get_grades_consensus(self, symbol: str) -> dict[str, Any]:
        """Get analyst grades consensus."""
        ...

    @abstractmethod
    async def get_grades_historical(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get historical analyst grades."""
        ...

    @abstractmethod
    async def get_price_target_consensus(self, symbol: str) -> dict[str, Any]:
        """Get price target consensus."""
        ...

    @abstractmethod
    async def get_price_target_summary(self, symbol: str) -> dict[str, Any]:
        """Get price target summary."""
        ...

    @abstractmethod
    async def get_price_target_news(
        self, symbol: str, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get price target news for a symbol."""
        ...

    @abstractmethod
    async def get_price_target_latest_news(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest price target news."""
        ...

    @abstractmethod
    async def get_grade_news(
        self, symbol: str, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get grade news for a symbol."""
        ...

    @abstractmethod
    async def get_grade_latest_news(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest grade news."""
        ...

    @abstractmethod
    async def get_latest_mergers_acquisitions(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest mergers & acquisitions."""
        ...

    @abstractmethod
    async def search_ma(self, query: str) -> dict[str, Any]:
        """Search for M&A deals by company name."""
        ...

    # =========================================================================
    # News (news.py) - 10 methods
    # =========================================================================

    @abstractmethod
    async def get_fmp_articles(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get FMP articles."""
        ...

    @abstractmethod
    async def get_general_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest general news."""
        ...

    @abstractmethod
    async def get_stock_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest stock news."""
        ...

    @abstractmethod
    async def search_stock_news_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search stock news by symbol."""
        ...

    @abstractmethod
    async def get_forex_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest forex news."""
        ...

    @abstractmethod
    async def search_forex_news_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search forex news by symbol."""
        ...

    @abstractmethod
    async def get_crypto_news_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest crypto news."""
        ...

    @abstractmethod
    async def search_crypto_news_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search crypto news by symbol."""
        ...

    @abstractmethod
    async def get_press_releases_latest(
        self,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get latest press releases."""
        ...

    @abstractmethod
    async def search_press_releases_by_symbol(
        self,
        symbol: str,
        page: int = 0,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Search press releases by symbol."""
        ...

    # =========================================================================
    # Market Performance (market_performance.py) - 12 methods
    # =========================================================================

    @abstractmethod
    async def get_sector_performance_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        sector: str | None = None,
    ) -> dict[str, Any]:
        """Get sector performance snapshot."""
        ...

    @abstractmethod
    async def get_industry_performance_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        industry: str | None = None,
    ) -> dict[str, Any]:
        """Get industry performance snapshot."""
        ...

    @abstractmethod
    async def get_historical_sector_performance(
        self,
        sector: str,
        from_date: str | None = None,
        to_date: str | None = None,
        exchange: str | None = None,
    ) -> dict[str, Any]:
        """Get historical sector performance."""
        ...

    @abstractmethod
    async def get_historical_industry_performance(
        self,
        industry: str,
        from_date: str | None = None,
        to_date: str | None = None,
        exchange: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical industry performance."""
        ...

    @abstractmethod
    async def get_sector_pe_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        sector: str | None = None,
    ) -> dict[str, Any]:
        """Get sector P/E ratio snapshot."""
        ...

    @abstractmethod
    async def get_industry_pe_snapshot(
        self,
        date: str,
        exchange: str | None = None,
        industry: str | None = None,
    ) -> dict[str, Any]:
        """Get industry P/E ratio snapshot."""
        ...

    @abstractmethod
    async def get_historical_sector_pe(
        self,
        sector: str,
        exchange: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical sector P/E ratios."""
        ...

    @abstractmethod
    async def get_historical_industry_pe(
        self,
        industry: str,
        exchange: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get historical industry P/E ratios."""
        ...

    @abstractmethod
    async def get_biggest_gainers(self) -> dict[str, Any]:
        """Get biggest gainers."""
        ...

    @abstractmethod
    async def get_biggest_losers(self) -> dict[str, Any]:
        """Get biggest losers."""
        ...

    @abstractmethod
    async def get_most_actives(self) -> dict[str, Any]:
        """Get most active stocks."""
        ...

    # =========================================================================
    # Earnings/Dividends/Splits (earnings_dividends_splits.py) - 5 methods
    # =========================================================================

    @abstractmethod
    async def get_earnings_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get earnings calendar."""
        ...

    @abstractmethod
    async def get_dividends_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get dividends calendar."""
        ...

    @abstractmethod
    async def get_splits_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get stock splits calendar."""
        ...

    @abstractmethod
    async def get_ipos_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get IPO calendar."""
        ...

    @abstractmethod
    async def get_ipos_prospectus(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get IPO prospectus data."""
        ...

    @abstractmethod
    async def get_ipos_disclosure(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get IPO disclosure data."""
        ...

    # =========================================================================
    # Earnings Transcript (earnings_transcript.py) - 5 methods
    # =========================================================================

    @abstractmethod
    async def get_earning_call_transcript(
        self, symbol: str, year: int, quarter: int
    ) -> dict[str, Any]:
        """Get earnings call transcript."""
        ...

    @abstractmethod
    async def get_transcript_dates_by_symbol(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get available transcript dates for a symbol."""
        ...

    @abstractmethod
    async def get_companies_with_transcripts(self, limit: int | None = None) -> dict[str, Any]:
        """Get companies with earnings transcripts."""
        ...

    @abstractmethod
    async def get_latest_earning_transcripts(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest earnings transcripts."""
        ...

    # =========================================================================
    # DCF Valuation (dcf_valuation.py) - 4 methods
    # =========================================================================

    @abstractmethod
    async def get_dcf_valuation(self, symbol: str) -> dict[str, Any]:
        """Get DCF valuation."""
        ...

    @abstractmethod
    async def get_levered_dcf_valuation(self, symbol: str) -> dict[str, Any]:
        """Get levered DCF valuation."""
        ...

    @abstractmethod
    async def get_custom_dcf_valuation(
        self,
        symbol: str,
        revenue_growth: float | None = None,
        ebitda_margin: float | None = None,
        cost_of_equity: float | None = None,
        terminal_growth: float | None = None,
        tax_rate: float | None = None,
        cost_of_debt: float | None = None,
        beta: float | None = None,
        risk_free_rate: float | None = None,
    ) -> dict[str, Any]:
        """Get custom DCF valuation with parameters."""
        ...

    @abstractmethod
    async def get_custom_levered_dcf_valuation(
        self,
        symbol: str,
        revenue_growth: float | None = None,
        ebitda_margin: float | None = None,
        cost_of_equity: float | None = None,
        terminal_growth: float | None = None,
        tax_rate: float | None = None,
        cost_of_debt: float | None = None,
        beta: float | None = None,
        risk_free_rate: float | None = None,
    ) -> dict[str, Any]:
        """Get custom levered DCF valuation with parameters."""
        ...

    # =========================================================================
    # Economics (economics.py) - 4 methods
    # =========================================================================

    @abstractmethod
    async def get_treasury_rates(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get treasury rates."""
        ...

    @abstractmethod
    async def get_economic_indicators(
        self,
        indicator: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get economic indicators."""
        ...

    @abstractmethod
    async def get_economic_calendar(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get economic calendar."""
        ...

    @abstractmethod
    async def get_market_risk_premium(self) -> dict[str, Any]:
        """Get market risk premium."""
        ...

    # =========================================================================
    # ETF/Mutual Funds (etf_mutual_funds.py) - 8 methods
    # =========================================================================

    @abstractmethod
    async def get_etf_holdings(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        """Get ETF holdings."""
        ...

    @abstractmethod
    async def get_etf_info(self, symbol: str) -> dict[str, Any]:
        """Get ETF information."""
        ...

    @abstractmethod
    async def get_etf_sector_weightings(self, symbol: str) -> dict[str, Any]:
        """Get ETF sector weightings."""
        ...

    @abstractmethod
    async def get_etf_country_weightings(self, symbol: str) -> dict[str, Any]:
        """Get ETF country weightings."""
        ...

    @abstractmethod
    async def get_etf_asset_exposure(self, symbol: str) -> dict[str, Any]:
        """Get ETF asset class exposure."""
        ...

    @abstractmethod
    async def get_fund_disclosure(
        self,
        symbol: str,
        year: str,
        quarter: str,
        cik: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Get fund disclosure."""
        ...

    @abstractmethod
    async def get_fund_disclosure_dates(
        self, symbol: str, cik: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get fund disclosure dates."""
        ...

    @abstractmethod
    async def get_fund_disclosure_holders_latest(
        self, symbol: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest fund disclosure holders."""
        ...

    @abstractmethod
    async def search_fund_disclosure_by_name(
        self, name: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Search fund disclosures by name."""
        ...

    # =========================================================================
    # Stock Directory (stock_directory.py) - 13 methods
    # =========================================================================

    @abstractmethod
    async def get_all_stock_symbols(self, limit: int | None = None) -> dict[str, Any]:
        """Get all stock symbols."""
        ...

    @abstractmethod
    async def get_all_etfs(self, limit: int | None = None) -> dict[str, Any]:
        """Get all ETF symbols."""
        ...

    @abstractmethod
    async def get_supported_exchanges(self) -> dict[str, Any]:
        """Get list of supported exchanges."""
        ...

    @abstractmethod
    async def find_exchange_listings(
        self, exchange: str, limit: int | None = None
    ) -> dict[str, Any]:
        """Find listings for an exchange."""
        ...

    @abstractmethod
    async def get_actively_trading_stocks(self, limit: int | None = None) -> dict[str, Any]:
        """Get actively trading stocks."""
        ...

    @abstractmethod
    async def get_delisted_companies(
        self, page: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """Get delisted companies."""
        ...

    @abstractmethod
    async def get_cik_database(self, page: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get CIK database."""
        ...

    @abstractmethod
    async def get_all_countries(self) -> dict[str, Any]:
        """Get all countries."""
        ...

    @abstractmethod
    async def get_all_sectors(self) -> dict[str, Any]:
        """Get all sectors."""
        ...

    @abstractmethod
    async def get_all_industries(self) -> dict[str, Any]:
        """Get all industries."""
        ...

    @abstractmethod
    async def get_recent_symbol_changes(
        self, invalid: bool | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get recent symbol changes."""
        ...

    @abstractmethod
    async def get_cryptocurrency_list(self, limit: int | None = None) -> dict[str, Any]:
        """Get cryptocurrency list."""
        ...

    @abstractmethod
    async def get_forex_currency_pairs(self, limit: int | None = None) -> dict[str, Any]:
        """Get forex currency pairs."""
        ...

    @abstractmethod
    async def get_commodities_list(self, limit: int | None = None) -> dict[str, Any]:
        """Get commodities list."""
        ...

    # =========================================================================
    # Indexes (indexes.py) - 7 methods
    # =========================================================================

    @abstractmethod
    async def get_index_list(self) -> dict[str, Any]:
        """Get list of available indexes."""
        ...

    @abstractmethod
    async def get_sp500_constituents(self, limit: int | None = None) -> dict[str, Any]:
        """Get S&P 500 constituents."""
        ...

    @abstractmethod
    async def get_nasdaq_constituents(self) -> dict[str, Any]:
        """Get NASDAQ constituents."""
        ...

    @abstractmethod
    async def get_dowjones_constituents(self) -> dict[str, Any]:
        """Get Dow Jones constituents."""
        ...

    @abstractmethod
    async def get_historical_sp500(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get historical S&P 500 constituents."""
        ...

    @abstractmethod
    async def get_historical_nasdaq(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get historical NASDAQ constituents."""
        ...

    @abstractmethod
    async def get_historical_dowjones(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get historical Dow Jones constituents."""
        ...

    # =========================================================================
    # Market Hours (market_hours.py) - 3 methods
    # =========================================================================

    @abstractmethod
    async def get_all_exchange_market_hours(self) -> dict[str, Any]:
        """Get market hours for all exchanges."""
        ...

    @abstractmethod
    async def get_exchange_market_hours(self, exchange: str) -> dict[str, Any]:
        """Get market hours for a specific exchange."""
        ...

    @abstractmethod
    async def get_holidays_by_exchange(
        self,
        exchange: str,
        from_date: str | None = None,
        to_date: str | None = None,
        year: int | None = None,
    ) -> dict[str, Any]:
        """Get holidays for an exchange."""
        ...

    # =========================================================================
    # Commodity (commodity.py) - 1 method (get_commodities_list is in stock_directory)
    # =========================================================================

    # Note: get_commodities_list is already defined in Stock Directory section

    # =========================================================================
    # Crypto (crypto.py) - 1 method (get_cryptocurrency_list is in stock_directory)
    # =========================================================================

    # Note: get_cryptocurrency_list is already defined in Stock Directory section

    # =========================================================================
    # Forex (forex.py) - 1 method (get_forex_currency_pairs is in stock_directory)
    # =========================================================================

    # Note: get_forex_currency_pairs is already defined in Stock Directory section

    # =========================================================================
    # Congressional Trading (congressional_trading.py) - 4 methods
    # =========================================================================

    @abstractmethod
    async def get_house_disclosure(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get House of Representatives financial disclosures."""
        ...

    @abstractmethod
    async def get_senate_disclosure(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get Senate financial disclosures."""
        ...

    @abstractmethod
    async def get_senate_trades(
        self, symbol: str, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get Senate trades for a symbol."""
        ...

    @abstractmethod
    async def get_house_trades(
        self, symbol: str, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get House trades for a symbol."""
        ...

    # =========================================================================
    # SEC Filings (sec_filings.py) - 2 methods
    # =========================================================================

    @abstractmethod
    async def get_institutional_ownership(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest institutional ownership filings."""
        ...

    @abstractmethod
    async def get_8k_filings(
        self, page: int | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Get latest 8-K filings."""
        ...
