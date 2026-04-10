"""Pydantic models for FMP MCP server."""

from mcp_schema import GeminiBaseModel as BaseModel
from mcp_schema import OutputBaseModel
from pydantic import Field, field_validator

# ============================================================================
# Request Models
# ============================================================================


class StockQuoteRequest(BaseModel):
    """Input specification for stock quote operations."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol (e.g., 'AAPL', 'MSFT', 'GOOGL')",
        examples=["AAPL", "MSFT", "GOOGL"],
        min_length=1,
        max_length=10,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class BatchStockQuotesRequest(BaseModel):
    """Input specification for batch stock quotes."""

    symbols: str = Field(
        ...,
        description="Comma-separated stock ticker symbols (e.g., 'AAPL,MSFT,GOOGL')",
        examples=["AAPL,MSFT,GOOGL", "TSLA,NVDA,AMD"],
        min_length=1,
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results to return (default: 50, max: 100)",
        ge=1,
        le=100,
    )


class StockQuoteShortRequest(BaseModel):
    """Input for short-form stock quote."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol (e.g., 'AAPL')",
        examples=["AAPL", "MSFT"],
        min_length=1,
        max_length=10,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class StockPriceChangeRequest(BaseModel):
    """Input for stock price change."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol (e.g., 'AAPL')",
        examples=["AAPL", "MSFT"],
        min_length=1,
        max_length=10,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class AftermarketQuoteRequest(BaseModel):
    """Input for aftermarket quote."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol (e.g., 'AAPL')",
        examples=["AAPL", "MSFT"],
        min_length=1,
        max_length=10,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class AftermarketTradeRequest(BaseModel):
    """Input for aftermarket trade."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol (e.g., 'AAPL')",
        examples=["AAPL", "MSFT"],
        min_length=1,
        max_length=10,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class BatchStockQuotesShortRequest(BaseModel):
    """Input for batch short-form stock quotes."""

    symbols: str = Field(
        ...,
        description="Comma-separated stock ticker symbols (e.g., 'AAPL,MSFT,GOOGL')",
        examples=["AAPL,MSFT,GOOGL"],
        min_length=1,
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )


class BatchAftermarketTradesRequest(BaseModel):
    """Input for batch aftermarket trades."""

    symbols: str = Field(
        ...,
        description="Comma-separated stock ticker symbols (e.g., 'AAPL,MSFT,GOOGL')",
        examples=["AAPL,MSFT,GOOGL"],
        min_length=1,
    )


class BatchAftermarketQuotesRequest(BaseModel):
    """Input for batch aftermarket quotes."""

    symbols: str = Field(
        ...,
        description="Comma-separated stock ticker symbols (e.g., 'AAPL,MSFT,GOOGL')",
        examples=["AAPL,MSFT,GOOGL"],
        min_length=1,
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )


class ExchangeStockQuotesRequest(BaseModel):
    """Input for exchange stock quotes."""

    exchange: str = Field(
        ...,
        description="Exchange code (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE", "AMEX"],
        min_length=1,
    )
    short: bool | None = Field(
        None,
        description="Return short format (default: false)",
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )


class AllQuotesRequest(BaseModel):
    """Input for retrieving all quotes of a specific asset type."""

    short: bool | None = Field(
        None,
        description="Return short format (default: false)",
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )


# ============================================================================
# Company Search Request Models
# ============================================================================


class SearchBySymbolRequest(BaseModel):
    """Input for symbol search."""

    query: str = Field(
        ...,
        description="Stock symbol or partial symbol (e.g., 'AAPL', 'AA')",
        examples=["AAPL", "MSFT", "GOOGL"],
        min_length=1,
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )
    exchange: str | None = Field(
        None,
        description="Filter by exchange (e.g., 'NASDAQ', 'NYSE')",
        examples=["NASDAQ", "NYSE"],
    )


class SearchByCompanyNameRequest(BaseModel):
    """Input for company name search."""

    query: str = Field(
        ...,
        description="Company name or partial name (e.g., 'Apple', 'Microsoft')",
        examples=["Apple", "Microsoft", "Tesla"],
        min_length=1,
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )
    exchange: str | None = Field(
        None,
        description="Filter by exchange (e.g., 'NASDAQ', 'NYSE')",
    )


class SearchByCikRequest(BaseModel):
    """Input for CIK search."""

    cik: str = Field(
        ...,
        description="CIK (Central Index Key) - SEC's unique identifier (e.g., '320193' for Apple)",
        examples=["320193", "789019"],
        min_length=1,
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )


class ScreenStocksRequest(BaseModel):
    """Input for stock screening/filtering."""

    market_cap_more_than: float | None = Field(
        None,
        description="Minimum market cap in millions (e.g., 1000.0 for $1 billion market cap)",
        ge=0,
    )
    market_cap_lower_than: float | None = Field(
        None,
        description="Maximum market cap in millions (e.g., 5000.0 for $5 billion market cap)",
        ge=0,
    )
    price_more_than: float | None = Field(
        None,
        description="Minimum stock price in USD (e.g., 10.50)",
        ge=0,
    )
    price_lower_than: float | None = Field(
        None,
        description="Maximum stock price in USD (e.g., 100.00)",
        ge=0,
    )
    beta_more_than: float | None = Field(
        None,
        description="Minimum beta value (e.g., 1.0 for stocks with market-level volatility)",
    )
    beta_lower_than: float | None = Field(
        None,
        description="Maximum beta value (e.g., 0.5 for low-volatility stocks)",
    )
    volume_more_than: int | None = Field(
        None,
        description="Minimum average trading volume (e.g., 1000000 for 1M shares)",
        ge=0,
    )
    volume_lower_than: int | None = Field(
        None,
        description="Maximum average trading volume",
        ge=0,
    )
    dividend_more_than: float | None = Field(
        None,
        description="Minimum dividend yield percentage (e.g., 2.0 for 2%)",
        ge=0,
    )
    dividend_lower_than: float | None = Field(
        None,
        description="Maximum dividend yield percentage",
        ge=0,
    )
    is_etf: bool | None = Field(
        None,
        description="Filter for ETFs only (true) or exclude ETFs (false)",
    )
    is_fund: bool | None = Field(
        None,
        description="Filter for funds only (true) or exclude funds (false)",
    )
    is_actively_trading: bool | None = Field(
        None,
        description="Filter for actively trading stocks only",
    )
    sector: str | None = Field(
        None,
        description="Sector filter (e.g., 'Technology', 'Healthcare')",
        examples=["Technology", "Healthcare", "Financial Services"],
    )
    industry: str | None = Field(
        None,
        description="Industry filter (e.g., 'Consumer Electronics', 'Software')",
        examples=["Consumer Electronics", "Software", "Biotechnology"],
    )
    country: str | None = Field(
        None,
        description="Country code (e.g., 'US', 'CA')",
        examples=["US", "CA", "GB"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange (e.g., 'NASDAQ', 'NYSE')",
        examples=["NASDAQ", "NYSE"],
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 1000)",
        ge=1,
        le=1000,
    )


class SearchByCusipRequest(BaseModel):
    """Input for CUSIP search."""

    cusip: str = Field(
        ...,
        description="CUSIP - US/Canada securities identifier (e.g., '037833100' for Apple)",
        examples=["037833100"],
        min_length=9,
        max_length=9,
    )


class SearchByIsinRequest(BaseModel):
    """Input for ISIN search."""

    isin: str = Field(
        ...,
        description=(
            "ISIN (International Securities Identification Number) (e.g., 'US0378331005' for Apple)"
        ),
        examples=["US0378331005"],
        min_length=12,
        max_length=12,
    )


class ExchangeListingsRequest(BaseModel):
    """Input for finding all stocks listed on an exchange."""

    exchange: str = Field(
        ...,
        description="Exchange code to find listings for (e.g., 'NASDAQ', 'NYSE')",
        examples=["NASDAQ", "NYSE", "AMEX"],
        min_length=1,
    )
    limit: int | None = Field(
        None,
        description="Maximum number of results to return (default: 50, max: 1000)",
        ge=1,
        le=1000,
    )


# ============================================================================
# Market Performance Request Models
# ============================================================================


class SectorPerformanceSnapshotRequest(BaseModel):
    """Input for sector performance snapshot."""

    date: str = Field(
        ...,
        description="Date in YYYY-MM-DD format",
        examples=["2025-01-15"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    sector: str | None = Field(
        None,
        description="Sector filter (e.g., 'Technology', 'Healthcare', 'Financial Services')",
        examples=["Technology", "Healthcare"],
    )


class IndustryPerformanceSnapshotRequest(BaseModel):
    """Input for industry performance snapshot."""

    date: str = Field(
        ...,
        description="Date in YYYY-MM-DD format",
        examples=["2025-01-15"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    industry: str | None = Field(
        None,
        description="Industry filter (e.g., 'Software', 'Biotechnology', 'Consumer Electronics')",
        examples=["Software", "Biotechnology"],
    )


class HistoricalSectorPerformanceRequest(BaseModel):
    """Input for historical sector performance."""

    sector: str = Field(
        ...,
        description="Sector name (e.g., 'Technology', 'Energy')",
        examples=["Technology", "Energy", "Healthcare"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")


class HistoricalIndustryPerformanceRequest(BaseModel):
    """Input for historical industry performance."""

    industry: str = Field(
        ...,
        description="Industry name (e.g., 'Biotechnology', 'Software')",
        examples=["Biotechnology", "Software"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")


class SectorPeSnapshotRequest(BaseModel):
    """Input for sector P/E snapshot."""

    date: str = Field(
        ...,
        description="Date in YYYY-MM-DD format",
        examples=["2025-01-15"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    sector: str | None = Field(
        None,
        description="Sector filter (e.g., 'Technology', 'Healthcare', 'Financial Services')",
        examples=["Technology", "Healthcare"],
    )


class IndustryPeSnapshotRequest(BaseModel):
    """Input for industry P/E snapshot."""

    date: str = Field(
        ...,
        description="Date in YYYY-MM-DD format",
        examples=["2025-01-15"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    industry: str | None = Field(
        None,
        description="Industry filter (e.g., 'Software', 'Biotechnology', 'Consumer Electronics')",
        examples=["Software", "Biotechnology"],
    )


class HistoricalSectorPeRequest(BaseModel):
    """Input for historical sector P/E."""

    sector: str = Field(
        ...,
        description="Sector name (e.g., 'Technology')",
        examples=["Technology", "Healthcare"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")


class HistoricalIndustryPeRequest(BaseModel):
    """Input for historical industry P/E."""

    industry: str = Field(
        ...,
        description="Industry name (e.g., 'Software')",
        examples=["Software", "Biotechnology"],
    )
    exchange: str | None = Field(
        None,
        description="Exchange filter (e.g., 'NASDAQ', 'NYSE', 'AMEX')",
        examples=["NASDAQ", "NYSE"],
    )
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")


class MarketMoversRequest(BaseModel):
    """Input for market movers (gainers/losers/actives)."""

    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )


# ============================================================================
# News Request Models
# ============================================================================


class FmpArticlesRequest(BaseModel):
    """Input for FMP articles."""

    page: int | None = Field(None, description="Page number (default: 0)", ge=0)
    limit: int | None = Field(
        None,
        description="Results per page (max: 20)",
        ge=1,
        le=20,
    )


class NewsLatestRequest(BaseModel):
    """Input for latest news (general, press releases, stock, crypto, forex)."""

    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")
    page: int | None = Field(None, description="Page number", ge=0)
    limit: int | None = Field(
        None,
        description="Results per page (max: 250)",
        ge=1,
        le=250,
    )


class NewsSearchBySymbolRequest(BaseModel):
    """Input for news search by symbol."""

    symbols: str = Field(
        ...,
        description="Symbol(s) to search for (e.g., 'AAPL', 'BTCUSD', 'EURUSD')",
        examples=["AAPL", "BTCUSD", "EURUSD"],
        min_length=1,
    )
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")
    page: int | None = Field(None, description="Page number", ge=0)
    limit: int | None = Field(
        None,
        description="Results per page (max: 250)",
        ge=1,
        le=250,
    )


# ============================================================================
# Company Information Request Models
# ============================================================================


class CompanySymbolRequest(BaseModel):
    """Input for company lookups by symbol."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol (e.g., 'AAPL')",
        examples=["AAPL", "MSFT", "GOOGL"],
        min_length=1,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class EtfSymbolRequest(BaseModel):
    """Input for ETF-specific requests."""

    symbol: str = Field(
        ...,
        description="ETF ticker symbol (e.g., 'VOO', 'SPY', 'QQQ')",
        examples=["VOO", "SPY", "QQQ"],
        min_length=1,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'VOO' not 'VOO,SPY')."
            )
        return v.strip().upper()


class CompanyCikRequest(BaseModel):
    """Input for company lookups by CIK."""

    cik: str = Field(
        ...,
        description="CIK number (e.g., '320193' for Apple)",
        examples=["320193"],
        min_length=1,
    )


class PaginatedRequest(BaseModel):
    """Input for paginated list requests."""

    page: int = Field(0, description="Page number (0-indexed, first page is 0)", ge=0)
    limit: int = Field(100, description="Results per page (default: 100, max: 1000)", ge=1, le=1000)


class EmployeeCountRequest(BaseModel):
    """Input for employee count requests."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    limit: int | None = Field(
        None, description="Maximum number of results to return (default: 10)", ge=1, le=100
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class HistoricalDataRequest(BaseModel):
    """Input for historical data requests."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    limit: int | None = Field(
        None, description="Maximum number of data points to return (default: 100)", ge=1, le=100
    )
    from_date: str | None = Field(
        None, description="Start date in YYYY-MM-DD format (e.g., '2024-01-01')"
    )
    to_date: str | None = Field(
        None, description="End date in YYYY-MM-DD format (e.g., '2024-12-31')"
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class BatchSymbolsRequest(BaseModel):
    """Input for batch symbol requests."""

    symbols: str = Field(
        ...,
        description="Comma-separated symbols (e.g., 'AAPL,MSFT,GOOGL')",
        examples=["AAPL,MSFT,GOOGL"],
        min_length=1,
    )


class SearchByNameRequest(BaseModel):
    """Input for searches by company name."""

    name: str = Field(
        ...,
        description="Company name to search",
        examples=["Apple", "Microsoft"],
        min_length=1,
    )


class FundNameSearchRequest(BaseModel):
    """Input for searches by fund name."""

    name: str = Field(
        ...,
        description="Fund name to search (e.g., 'Vanguard', 'Fidelity')",
        examples=["Vanguard", "Fidelity", "BlackRock"],
        min_length=1,
    )


class CompanyExecutivesRequest(BaseModel):
    """Input for company executives request."""

    symbol: str = Field(..., description="Stock ticker symbol", min_length=1)
    active: bool | None = Field(None, description="Filter to only active executives")

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class ExecutiveCompBenchmarkRequest(BaseModel):
    """Input for executive compensation benchmark."""

    year: str | None = Field(
        None,
        description="Filter by year. Year filter (e.g., '2023')",
        examples=["2023", "2022"],
    )


# ============================================================================
# Asset Directory Request Models
# ============================================================================


class AssetListRequest(BaseModel):
    """Input for asset list requests (commodities, crypto, forex)."""

    limit: int | None = Field(
        None,
        description="Maximum number of results (default: 50, max: 100)",
        ge=1,
        le=100,
    )


# ============================================================================
# Market Hours Request Models
# ============================================================================


class ExchangeRequest(BaseModel):
    """Input for exchange-specific requests."""

    exchange: str = Field(
        ...,
        description="Exchange code (e.g., 'NASDAQ', 'NYSE', 'LSE')",
        examples=["NASDAQ", "NYSE", "LSE"],
        min_length=1,
    )


class ExchangeHolidaysRequest(BaseModel):
    """Input for exchange holidays request."""

    exchange: str = Field(
        ...,
        description="Exchange code (e.g., 'NASDAQ', 'NYSE')",
        examples=["NASDAQ", "NYSE"],
        min_length=1,
    )
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")


# ============================================================================
# DCF Valuation Request Models
# ============================================================================


class CustomDcfRequest(BaseModel):
    """Input for custom DCF valuation."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()

    revenue_growth_pct: float | None = Field(
        None, description="Expected revenue growth rate as decimal (e.g., 0.10 for 10%)"
    )
    ebitda_pct: float | None = Field(
        None, description="EBITDA margin as decimal (e.g., 0.25 for 25%)"
    )
    tax_rate: float | None = Field(
        None, description="Corporate tax rate as decimal (e.g., 0.21 for 21%)"
    )
    long_term_growth_rate: float | None = Field(
        None, description="Terminal growth rate as decimal (e.g., 0.025 for 2.5%)"
    )
    cost_of_debt: float | None = Field(
        None, description="Cost of debt as decimal (e.g., 0.05 for 5%)"
    )
    cost_of_equity: float | None = Field(
        None, description="Cost of equity as decimal (e.g., 0.10 for 10%)"
    )
    beta: float | None = Field(
        None, description="Stock beta relative to market (e.g., 1.2 means 20% more volatile)"
    )
    risk_free_rate: float | None = Field(
        None, description="Risk-free rate as decimal (e.g., 0.04 for 4% Treasury rate)"
    )


# ============================================================================
# Earnings Transcript Request Models
# ============================================================================


class EarningTranscriptRequest(BaseModel):
    """Input for specific earning call transcript."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    year: str = Field(
        ...,
        description="Fiscal year as 4-digit string (e.g., '2023', '2024')",
        examples=["2023", "2024"],
    )
    quarter: str = Field(
        ...,
        description="Fiscal quarter as single digit: '1', '2', '3', or '4'",
        examples=["1", "2", "3", "4"],
    )
    limit: int | None = Field(
        None, description="Maximum number of results to return (default: 10)", ge=1, le=100
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class TranscriptListRequest(BaseModel):
    """Input for transcript list requests."""

    limit: int | None = Field(
        None, description="Maximum results per page (default: 20)", ge=1, le=100
    )
    page: int | None = Field(
        None, description="Page number (0-indexed, first page is 0)", ge=0, le=100
    )


# ============================================================================
# Economics Request Models
# ============================================================================


class DateRangeRequest(BaseModel):
    """Input for date range requests."""

    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD, max 90-day range)")


class EconomicIndicatorRequest(BaseModel):
    """Input for economic indicator request."""

    name: str = Field(
        ...,
        description="Indicator name (e.g., 'GDP', 'CPI', 'unemploymentRate', 'inflationRate')",
        examples=["GDP", "CPI", "unemploymentRate", "inflationRate"],
        min_length=1,
    )
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD)")


class EconomicCalendarRequest(BaseModel):
    """Input for economic calendar request."""

    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD)")
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD, max 90-day range)")
    limit: int | None = Field(None, description="Max results", ge=1, le=100)


# ============================================================================
# Empty Request Models (for endpoints with no params)
# ============================================================================


class EmptyRequest(BaseModel):
    """Input for endpoints that take no parameters."""

    pass


# ============================================================================
# Fund/ETF Request Models
# ============================================================================


class FundDisclosureRequest(BaseModel):
    """Input for fund disclosure request."""

    symbol: str = Field(
        ...,
        description="Fund/ETF ticker symbol in uppercase (e.g., 'VOO', 'SPY', 'QQQ')",
        examples=["VOO", "SPY", "QQQ"],
        min_length=1,
    )
    year: str = Field(
        ...,
        description="Fiscal year as 4-digit string (e.g., '2023', '2024')",
        examples=["2023", "2024"],
    )
    quarter: str = Field(
        ...,
        description="Fiscal quarter as single digit: '1', '2', '3', or '4'",
        examples=["1", "2", "3", "4"],
    )
    cik: str | None = Field(
        None,
        description="SEC CIK number (optional, use if symbol lookup fails)",
        examples=["320193"],
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'VOO' not 'VOO,SPY')."
            )
        return v.strip().upper()


class FundDisclosureDatesRequest(BaseModel):
    """Input for fund disclosure dates request."""

    symbol: str = Field(..., description="Fund/ETF symbol", min_length=1)
    cik: str | None = Field(None, description="CIK number (optional)")

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'VOO' not 'VOO,SPY')."
            )
        return v.strip().upper()


# ============================================================================
# Stock Directory Request Models
# ============================================================================


class SymbolChangesRequest(BaseModel):
    """Input for symbol changes request."""

    invalid: bool | None = Field(None, description="Filter to show only invalid symbols")
    limit: int | None = Field(None, description="Max results", ge=1, le=100)


class PaginatedCikRequest(BaseModel):
    """Input for CIK database paginated request."""

    page: int = Field(0, description="Page number (0-indexed, first page is 0)", ge=0)
    limit: int = Field(
        1000, description="Results per page (default: 1000, max: 1000)", ge=1, le=1000
    )


# ============================================================================
# Chart/Historical Data Request Models
# ============================================================================


class HistoricalPriceRequest(BaseModel):
    """Input for historical price data."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    from_date: str | None = Field(
        None, description="Start date in YYYY-MM-DD format (e.g., '2024-01-01')"
    )
    to_date: str | None = Field(
        None, description="End date in YYYY-MM-DD format (e.g., '2024-12-31')"
    )
    limit: int | None = Field(
        None, description="Maximum number of data points to return (default: 100)", ge=1, le=1000
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class IntradayRequest(BaseModel):
    """Input for intraday price data."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    from_date: str | None = Field(
        None,
        description=(
            "Start date in YYYY-MM-DD format ONLY (e.g., '2024-01-01'). "
            "Do NOT include time - returns all intraday data for the date range."
        ),
    )
    to_date: str | None = Field(
        None,
        description=(
            "End date in YYYY-MM-DD format ONLY (e.g., '2024-12-31'). "
            "Do NOT include time - returns all intraday data for the date range."
        ),
    )
    nonadjusted: bool | None = Field(
        None,
        description=(
            "If true, return raw prices without split/dividend adjustments (default: false)"
        ),
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


# ============================================================================
# Technical Indicator Request Models
# ============================================================================


class TechnicalIndicatorRequest(BaseModel):
    """Input for technical indicators."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    periodLength: int = Field(
        ...,
        description="Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA)",
        examples=[10, 14, 20, 50, 200],
        ge=1,
        le=200,
    )
    timeframe: str = Field(
        ...,
        description="Chart timeframe: '1min', '5min', '15min', '30min', '1hour', '4hour', '1day'",
        examples=["1day", "1hour", "5min"],
    )
    from_date: str | None = Field(
        None, description="Start date in YYYY-MM-DD format (e.g., '2024-01-01')"
    )
    to_date: str | None = Field(
        None, description="End date in YYYY-MM-DD format (e.g., '2024-12-31')"
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


# ============================================================================
# Analyst/Rating Request Models
# ============================================================================


class AnalystPagedRequest(BaseModel):
    """Input for analyst data with pagination."""

    page: int | None = Field(None, description="Page number (0-indexed, first page is 0)", ge=0)
    limit: int | None = Field(
        None, description="Maximum number of results to return (default: 50)", ge=1, le=100
    )


# ============================================================================
# Financial Statement Request Models
# ============================================================================


class FinancialStatementRequest(BaseModel):
    """Input for financial statement data."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    period: str = Field(
        ...,
        description="Frequency of data: 'annual' for yearly statements, 'quarter' for quarterly",
        examples=["annual", "quarter"],
    )
    limit: int | None = Field(
        None, description="Maximum number of periods to return (default: 5)", ge=1, le=100
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class FinancialReportRequest(BaseModel):
    """Input for financial report request."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT')",
        examples=["AAPL", "MSFT"],
        min_length=1,
    )
    year: str = Field(
        ...,
        description="Fiscal year as 4-digit string (e.g., '2023', '2024')",
        examples=["2023", "2024"],
    )
    period: str = Field(
        ...,
        description="Reporting period: 'Q1', 'Q2', 'Q3', 'Q4' for quarters, or 'FY' for full year",
        examples=["Q1", "Q2", "Q3", "Q4", "FY"],
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


class LargePagedRequest(BaseModel):
    """Input for large paginated requests."""

    page: int = Field(0, description="Page number (0-indexed, first page is 0)", ge=0)
    limit: int = Field(250, description="Results per page (default: 250, max: 250)", ge=1, le=250)


# ============================================================================
# Stock Quote Models
# ============================================================================


class StockQuote(OutputBaseModel):
    """Model for a stock quote."""

    symbol: str = Field(..., description="Stock ticker symbol")
    name: str | None = Field(None, description="Company name")
    price: float | None = Field(None, description="Current stock price")
    changesPercentage: float = Field(default=0.0, description="Price change percentage")
    change: float = Field(default=0.0, description="Price change amount")
    dayLow: float | None = Field(None, description="Day's low price")
    dayHigh: float | None = Field(None, description="Day's high price")
    yearHigh: float | None = Field(None, description="52-week high price")
    yearLow: float | None = Field(None, description="52-week low price")
    marketCap: int | None = Field(None, description="Market capitalization")
    priceAvg50: float | None = Field(None, description="50-day average price")
    priceAvg200: float | None = Field(None, description="200-day average price")
    volume: int | None = Field(None, description="Trading volume")
    avgVolume: int | None = Field(None, description="Average trading volume")
    exchange: str | None = Field(None, description="Stock exchange")
    open: float | None = Field(None, description="Opening price")
    previousClose: float | None = Field(None, description="Previous close price")
    eps: float | None = Field(None, description="Earnings per share")
    pe: float | None = Field(None, description="Price-to-earnings ratio")
    earningsAnnouncement: str | None = Field(None, description="Earnings announcement date")
    sharesOutstanding: int | None = Field(None, description="Shares outstanding")
    timestamp: int | None = Field(None, description="Timestamp of quote")

    def __str__(self) -> str:
        """Format stock quote for LLM consumption."""
        lines = [
            f"Symbol: {self.symbol}",
            f"Name: {self.name}",
            f"Price: ${self.price:.2f}",
            f"Change: ${self.change:.2f} ({self.changesPercentage:.2f}%)",
            f"Day Range: ${self.dayLow:.2f} - ${self.dayHigh:.2f}",
            f"52-Week Range: ${self.yearLow:.2f} - ${self.yearHigh:.2f}",
        ]

        if self.marketCap:
            lines.append(f"Market Cap: ${self.marketCap:,}")
        if self.volume:
            lines.append(f"Volume: {self.volume:,}")
        if self.avgVolume:
            lines.append(f"Avg Volume: {self.avgVolume:,}")
        if self.exchange:
            lines.append(f"Exchange: {self.exchange}")
        if self.open:
            lines.append(f"Open: ${self.open:.2f}")
        if self.previousClose:
            lines.append(f"Previous Close: ${self.previousClose:.2f}")
        if self.eps:
            lines.append(f"EPS: ${self.eps:.2f}")
        if self.pe:
            lines.append(f"P/E Ratio: {self.pe:.2f}")
        if self.sharesOutstanding:
            lines.append(f"Shares Outstanding: {self.sharesOutstanding:,}")

        return "\n".join(lines)


class StockQuoteResponse(OutputBaseModel):
    """Response model for stock quote operations."""

    symbol: str = Field(..., description="Stock ticker symbol")
    quote: StockQuote | None = Field(None, description="Stock quote data")
    error: str | None = Field(None, description="Error message if operation failed")

    def __str__(self) -> str:
        """Format stock quote response for LLM consumption."""
        if self.error:
            return f"Error fetching quote for {self.symbol}: {self.error}"
        if not self.quote:
            return f"No quote data available for {self.symbol}"
        return str(self.quote)


# ============================================================================
# Company Profile Models
# ============================================================================


class CompanyProfile(OutputBaseModel):
    """Model for company profile information."""

    symbol: str = Field(..., description="Stock ticker symbol")
    price: float = Field(..., description="Current stock price")
    beta: float | None = Field(None, description="Beta value")
    volAvg: int | None = Field(None, description="Average volume")
    mktCap: int | None = Field(None, description="Market capitalization")
    lastDiv: float | None = Field(None, description="Last dividend")
    range: str | None = Field(None, description="52-week price range")
    changes: float | None = Field(None, description="Price change")
    companyName: str | None = Field(None, description="Company name")
    currency: str | None = Field(None, description="Currency")
    cik: str | None = Field(None, description="CIK number")
    isin: str | None = Field(None, description="ISIN")
    cusip: str | None = Field(None, description="CUSIP")
    exchange: str | None = Field(None, description="Stock exchange")
    exchangeShortName: str | None = Field(None, description="Exchange short name")
    industry: str | None = Field(None, description="Industry")
    website: str | None = Field(None, description="Company website")
    description: str | None = Field(None, description="Company description")
    ceo: str | None = Field(None, description="CEO name")
    sector: str | None = Field(None, description="Sector")
    country: str | None = Field(None, description="Country")
    fullTimeEmployees: str | None = Field(None, description="Full-time employees")
    phone: str | None = Field(None, description="Phone number")
    address: str | None = Field(None, description="Company address")
    city: str | None = Field(None, description="City")
    state: str | None = Field(None, description="State")
    zip: str | None = Field(None, description="ZIP code")
    dcfDiff: float | None = Field(None, description="DCF difference")
    dcf: float | None = Field(None, description="DCF value")
    image: str | None = Field(None, description="Company logo URL")
    ipoDate: str | None = Field(None, description="IPO date")
    defaultImage: bool | None = Field(None, description="Default image flag")
    isEtf: bool | None = Field(None, description="Is ETF flag")
    isActivelyTrading: bool | None = Field(None, description="Is actively trading flag")

    def __str__(self) -> str:
        """Format company profile for LLM consumption."""
        lines = [
            f"Symbol: {self.symbol}",
            f"Company Name: {self.companyName or 'N/A'}",
            f"Price: ${self.price:.2f}",
        ]

        if self.sector:
            lines.append(f"Sector: {self.sector}")
        if self.industry:
            lines.append(f"Industry: {self.industry}")
        if self.exchange:
            lines.append(f"Exchange: {self.exchange}")
        if self.mktCap:
            lines.append(f"Market Cap: ${self.mktCap:,}")
        if self.beta:
            lines.append(f"Beta: {self.beta:.2f}")
        if self.description:
            lines.append(f"\nDescription: {self.description}")
        if self.ceo:
            lines.append(f"CEO: {self.ceo}")
        if self.country:
            lines.append(f"Country: {self.country}")
        if self.website:
            lines.append(f"Website: {self.website}")
        if self.address:
            address_parts = [self.address]
            if self.city:
                address_parts.append(self.city)
            if self.state:
                address_parts.append(self.state)
            if self.zip:
                address_parts.append(self.zip)
            lines.append(f"Address: {', '.join(address_parts)}")
        if self.ipoDate:
            lines.append(f"IPO Date: {self.ipoDate}")

        return "\n".join(lines)


class CompanyProfileResponse(OutputBaseModel):
    """Response model for company profile operations."""

    symbol: str = Field(..., description="Stock ticker symbol")
    profile: CompanyProfile | None = Field(None, description="Company profile data")
    error: str | None = Field(None, description="Error message if operation failed")

    def __str__(self) -> str:
        """Format company profile response for LLM consumption."""
        if self.error:
            return f"Error fetching profile for {self.symbol}: {self.error}"
        if not self.profile:
            return f"No profile data available for {self.symbol}"
        return str(self.profile)


# ============================================================================
# Financial Statement Models
# ============================================================================


class FinancialStatement(OutputBaseModel):
    """Base model for financial statements."""

    date: str = Field(..., description="Statement date")
    symbol: str = Field(..., description="Stock ticker symbol")
    reportedCurrency: str | None = Field(None, description="Reporting currency")
    cik: str | None = Field(None, description="CIK number")
    fillingDate: str | None = Field(None, description="Filing date")
    acceptedDate: str | None = Field(None, description="Accepted date")
    period: str | None = Field(None, description="Period (Q or FY)")
    link: str | None = Field(None, description="Link to filing")
    finalLink: str | None = Field(None, description="Final link to filing")

    def __str__(self) -> str:
        """Format financial statement for LLM consumption."""
        lines = [
            f"Symbol: {self.symbol}",
            f"Date: {self.date}",
        ]
        if self.period:
            lines.append(f"Period: {self.period}")
        if self.reportedCurrency:
            lines.append(f"Currency: {self.reportedCurrency}")
        return "\n".join(lines)


class FinancialStatementResponse(OutputBaseModel):
    """Response model for financial statement operations."""

    symbol: str = Field(..., description="Stock ticker symbol")
    statement_type: str = Field(..., description="Type of financial statement")
    statements: list[dict] = Field(default_factory=list, description="List of financial statements")
    error: str | None = Field(None, description="Error message if operation failed")

    def __str__(self) -> str:
        """Format financial statement response for LLM consumption."""
        if self.error:
            return f"Error fetching {self.statement_type} for {self.symbol}: {self.error}"
        if not self.statements:
            return f"No {self.statement_type} data available for {self.symbol}"

        lines = [
            f"Symbol: {self.symbol}",
            f"Statement Type: {self.statement_type}",
            f"Number of Periods: {len(self.statements)}",
            "",
            "=" * 80,
            "",
        ]

        # Show first few statements
        for idx, statement in enumerate(self.statements[:5], 1):
            lines.append(f"Period {idx}:")
            lines.append("-" * 80)
            for key, value in statement.items():
                if isinstance(value, int | float):
                    lines.append(f"{key}: {value:,}")
                else:
                    lines.append(f"{key}: {value}")
            lines.append("")

        if len(self.statements) > 5:
            lines.append(f"... and {len(self.statements) - 5} more periods")

        return "\n".join(lines)


# ============================================================================
# Stock Search Models
# ============================================================================


class StockSearchResult(OutputBaseModel):
    """Model for a stock search result."""

    symbol: str = Field(..., description="Stock ticker symbol")
    name: str = Field(..., description="Company name")
    currency: str | None = Field(None, description="Currency")
    stockExchange: str | None = Field(None, description="Stock exchange")
    exchangeShortName: str | None = Field(None, description="Exchange short name")

    def __str__(self) -> str:
        """Format search result for LLM consumption."""
        lines = [f"{self.symbol} - {self.name}"]
        if self.stockExchange:
            lines.append(f"  Exchange: {self.stockExchange}")
        if self.currency:
            lines.append(f"  Currency: {self.currency}")
        return "\n".join(lines)


class StockSearchResponse(OutputBaseModel):
    """Response model for stock search operations."""

    query: str = Field(..., description="Search query")
    results: list[StockSearchResult] = Field(
        default_factory=list, description="List of search results"
    )
    total_count: int = Field(..., description="Total number of results")
    error: str | None = Field(None, description="Error message if search failed")

    def __str__(self) -> str:
        """Format search response for LLM consumption."""
        if self.error:
            return f"Search Error: {self.error}"

        lines = [
            f"Search Query: {self.query}",
            f"Total Results: {self.total_count} (showing {len(self.results)})",
            "",
            "=" * 80,
            "",
        ]

        for idx, result in enumerate(self.results, 1):
            lines.append(f"Result {idx}:")
            lines.append("-" * 80)
            lines.append(str(result))
            lines.append("")

        return "\n".join(lines)


# ============================================================================
# Data Management Request Models
# ============================================================================


class LoadBundledFixturesRequest(BaseModel):
    """Input for loading bundled fixtures into the database.

    This tool loads pre-bundled fixture data for offline operation.
    No API key or internet connection is required.
    """

    force: bool = Field(
        default=False,
        description=(
            "Clear existing data before loading (fresh start). "
            "Default is False to preserve existing data."
        ),
    )


# ============================================================================
# Congressional Trading Request Models
# ============================================================================


class CongressionalDisclosureRequest(BaseModel):
    """Input for congressional disclosure requests (House/Senate latest)."""

    page: int = Field(0, description="Page number (0-indexed, first page is 0)", ge=0)
    limit: int | None = Field(
        None,
        description="Maximum number of results to return (default: 50, max: 100)",
        ge=1,
        le=100,
    )


class CongressionalTradesRequest(BaseModel):
    """Input for congressional trades by symbol."""

    symbol: str = Field(
        ...,
        description="Stock ticker symbol in uppercase (e.g., 'AAPL', 'MSFT', 'GOOGL')",
        examples=["AAPL", "MSFT", "GOOGL"],
        min_length=1,
        max_length=10,
    )
    page: int = Field(0, description="Page number (0-indexed, first page is 0)", ge=0)
    limit: int | None = Field(
        None,
        description="Maximum number of results to return (default: 50, max: 100)",
        ge=1,
        le=100,
    )

    @field_validator("symbol")
    @classmethod
    def validate_single_symbol(cls, v: str) -> str:
        if "," in v:
            raise ValueError(
                "This endpoint accepts a single ticker symbol. "
                "Please query one symbol at a time (e.g., 'AAPL' not 'AAPL,MSFT')."
            )
        return v.strip().upper()


# ============================================================================
# SEC Filings Request Models
# ============================================================================


class SecFilingsLatestRequest(BaseModel):
    """Input for latest SEC filings requests (8K, institutional ownership)."""

    page: int = Field(0, description="Page number (0-indexed, first page is 0)", ge=0)
    limit: int | None = Field(
        None,
        description="Maximum number of results to return (default: 50, max: 100)",
        ge=1,
        le=100,
    )
