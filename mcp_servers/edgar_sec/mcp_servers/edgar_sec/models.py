"""Pydantic models for EDGAR SEC MCP server."""

from typing import Literal

from mcp_schema import GeminiBaseModel as BaseModel
from mcp_schema import OutputBaseModel
from pydantic import Field, model_validator


# Lookup models
class LookupCIKInput(BaseModel):
    """Input model for lookup_cik tool."""

    ticker: str | None = Field(None, description="Stock ticker symbol (e.g., 'AAPL')")
    name: str | None = Field(None, description="Company name (e.g., 'Apple Inc')")

    @model_validator(mode="after")
    def check_at_least_one(self):
        """Validate that at least one of ticker or name is provided."""
        if not self.ticker and not self.name:
            raise ValueError("Must provide either ticker or name")
        return self


class LookupCIKOutput(OutputBaseModel):
    """Output model for lookup_cik tool."""

    cik: str = Field(..., description="10-digit zero-padded CIK")
    name: str = Field(..., description="Official company name")
    ticker: str | None = Field(None, description="Primary ticker symbol")
    exchange: str | None = Field(None, description="Primary exchange (e.g., 'Nasdaq')")


# Equity compensation models
class StockOptionActivity(OutputBaseModel):
    """Stock option activity data."""

    outstanding_beginning: int | None = Field(
        None, description="Number of options outstanding at start of period"
    )
    granted: int | None = Field(None, description="Number of options granted during period")
    exercised: int | None = Field(
        None,
        description=(
            "Number of options exercised during period. "
            "Positive integer representing reduction from outstanding."
        ),
    )
    forfeited: int | None = Field(
        None,
        description=(
            "Number of options forfeited/cancelled during period. "
            "Positive integer representing reduction from outstanding."
        ),
    )
    expired: int | None = Field(
        None,
        description=(
            "Number of options expired during period. "
            "Positive integer representing reduction from outstanding."
        ),
    )
    outstanding_ending: int | None = Field(
        None, description="Number of options outstanding at end of period"
    )
    exercisable_ending: int | None = Field(
        None, description="Number of options exercisable at end of period"
    )
    weighted_avg_exercise_price_beginning: float | None = Field(
        None, description="Weighted avg exercise price at start"
    )
    weighted_avg_exercise_price_granted: float | None = Field(
        None, description="Weighted avg exercise price of grants"
    )
    weighted_avg_exercise_price_exercised: float | None = Field(
        None, description="Weighted avg exercise price of exercises"
    )
    weighted_avg_exercise_price_ending: float | None = Field(
        None, description="Weighted avg exercise price at end"
    )


class RSUActivity(OutputBaseModel):
    """Restricted Stock Unit activity data."""

    unvested_beginning: int | None = Field(
        None, description="Number of unvested RSUs at start of period"
    )
    granted: int | None = Field(None, description="Number of RSUs granted during period")
    vested: int | None = Field(
        None,
        description=(
            "Number of RSUs vested during period. "
            "Positive integer representing reduction from unvested."
        ),
    )
    forfeited: int | None = Field(
        None,
        description=(
            "Number of RSUs forfeited/cancelled during period. "
            "Positive integer representing reduction from unvested."
        ),
    )
    unvested_ending: int | None = Field(
        None, description="Number of unvested RSUs at end of period"
    )
    weighted_avg_grant_date_fair_value_beginning: float | None = Field(
        None, description="Weighted avg fair value at start"
    )
    weighted_avg_grant_date_fair_value_ending: float | None = Field(
        None, description="Weighted avg fair value at end"
    )


class PSUActivity(OutputBaseModel):
    """Performance Share Unit activity data."""

    unvested_beginning: int | None = Field(
        None, description="Number of unvested PSUs at start of period"
    )
    granted: int | None = Field(None, description="Number of PSUs granted during period")
    vested: int | None = Field(
        None,
        description=(
            "Number of PSUs vested during period. "
            "Positive integer representing reduction from unvested."
        ),
    )
    forfeited: int | None = Field(
        None,
        description=(
            "Number of PSUs forfeited/cancelled during period. "
            "Positive integer representing reduction from unvested."
        ),
    )
    unvested_ending: int | None = Field(
        None, description="Number of unvested PSUs at end of period"
    )
    weighted_avg_grant_date_fair_value: float | None = Field(
        None, description="Weighted avg grant date fair value"
    )


class ESPPActivity(OutputBaseModel):
    """Employee Stock Purchase Plan activity data."""

    shares_available: int | None = Field(None, description="Shares available for purchase")
    shares_purchased: int | None = Field(None, description="Shares purchased during period")
    weighted_avg_purchase_price: float | None = Field(
        None, description="Weighted avg purchase price"
    )


class GetEquityCompensationInput(BaseModel):
    """Input model for get_equity_compensation tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(None, description="Stock ticker symbol (e.g., 'AAPL')")
    name: str | None = Field(None, description="Company name (e.g., 'Apple Inc')")
    filing_accession: str = Field(
        ...,
        description=(
            "SEC filing accession number in format 'XXXXXXXXXX-XX-XXXXXX' "
            "(e.g., '0000320193-24-000081'). "
            "IMPORTANT: This value must be obtained from edgar_filings action='submissions' "
            "response - it appears in the 'accession_number' field of each filing record. "
            "Do NOT fabricate this value."
        ),
        json_schema_extra={
            "x-populate-from": "get_company_submissions",
            "x-populate-field": "filings",
            "x-populate-value": "accession_number",
            "x-populate-display": "{form} ({filing_date}) {accession_number}",
            "x-populate-dependencies": {"ticker": "ticker"},
        },
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class GetEquityCompensationOutput(OutputBaseModel):
    """Output model for get_equity_compensation tool."""

    cik: str = Field(..., description="10-digit zero-padded CIK")
    filing_accession: str = Field(..., description="Filing accession number")
    report_date: str | None = Field(None, description="Report period end date (YYYY-MM-DD)")
    stock_options: StockOptionActivity | None = Field(
        None, description="Stock option activity data"
    )
    rsus: RSUActivity | None = Field(None, description="RSU activity data")
    psus: PSUActivity | None = Field(None, description="PSU activity data")
    espp: ESPPActivity | None = Field(None, description="ESPP activity data")
    data_source: Literal["xbrl_api", "xbrl_parser", "html_scraper"] = Field(
        ..., description="Which retrieval tier succeeded"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        ...,
        description=(
            "Data extraction reliability indicator. "
            "'high': From structured XBRL API - highly reliable. "
            "'medium': From parsed XBRL files - generally accurate. "
            "'low': From HTML scraping - may have errors, consider manual verification "
            "by fetching the source document via edgar_documents."
        ),
    )


# Filing tool models
class GetCompanySubmissionsInput(BaseModel):
    """Input model for get_company_submissions tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(None, description="Stock ticker symbol (e.g., 'AAPL')")
    name: str | None = Field(None, description="Company name (e.g., 'Apple Inc')")
    limit: int | None = Field(20, description="Max number of filings to return per page (max: 50)")
    page: int | None = Field(1, description="Page number (1-indexed) for pagination")
    form_types: list[str] | None = Field(
        None,
        description=(
            "Filter by SEC form types as array (e.g., ['10-K', '10-Q']). "
            "Common types: '10-K' (annual), '10-Q' (quarterly), '8-K' (current events), "
            "'DEF 14A' (proxy), 'S-1' (registration), '4' (insider transactions), "
            "'13F-HR' (institutional holdings). "
            "For amendments, add '/A' suffix: '10-K/A', '10-Q/A'. "
            "Case-insensitive matching. If null, returns all form types."
        ),
    )
    summary_only: bool = Field(
        False,
        description=(
            "If True, return only summary statistics (filing counts by form type, date ranges) "
            "without full filing details. Use this to understand what's available before "
            "requesting specific filings."
        ),
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class FilingRecord(OutputBaseModel):
    """A single filing record with essential fields."""

    accession_number: str = Field(
        ..., description="Filing accession number (e.g., '0000320193-24-000123')"
    )
    form: str = Field(..., description="SEC form type (e.g., '10-K', '10-Q', '8-K')")
    filing_date: str = Field(..., description="Date filed with SEC (YYYY-MM-DD)")
    report_date: str | None = Field(None, description="Period of report (YYYY-MM-DD)")
    primary_document: str | None = Field(None, description="Primary document filename")
    description: str | None = Field(None, description="Filing description")


class PaginationInfo(OutputBaseModel):
    """Pagination metadata for paginated responses."""

    page: int = Field(..., description="Current page number (1-indexed)")
    page_size: int = Field(..., description="Number of items per page")
    total_items: int = Field(..., description="Total number of items available")
    total_pages: int = Field(..., description="Total number of pages")
    has_next: bool = Field(..., description="Whether there are more pages")
    has_previous: bool = Field(..., description="Whether there are previous pages")


class FormTypeSummary(OutputBaseModel):
    """Summary of filings by form type."""

    form_type: str = Field(..., description="SEC form type")
    count: int = Field(..., description="Number of filings of this type")
    earliest_date: str | None = Field(None, description="Earliest filing date")
    latest_date: str | None = Field(None, description="Most recent filing date")


class GetCompanySubmissionsOutput(OutputBaseModel):
    """Output model for get_company_submissions tool."""

    company_name: str = Field(..., description="Official company name")
    cik: str = Field(..., description="10-digit zero-padded CIK")
    sic: str | None = Field(None, description="Standard Industrial Classification code")
    sic_description: str | None = Field(None, description="SIC code description")

    # Summary statistics (always included)
    total_filings: int = Field(..., description="Total number of filings for this company")
    form_type_summary: list[FormTypeSummary] = Field(
        ..., description="Filing counts broken down by form type"
    )

    # Paginated filings (empty if summary_only=True)
    filings: list[FilingRecord] = Field(
        default_factory=list,
        description="List of filings for the current page (empty if summary_only=True)",
    )

    # Pagination info
    pagination: PaginationInfo | None = Field(
        None, description="Pagination info (None if summary_only=True)"
    )

    # Filter info
    filter_applied: str | None = Field(
        None, description="Description of any filters applied (e.g., 'form_types: 10-K, 10-Q')"
    )


class GetCompanyFactsInput(BaseModel):
    """Input model for get_company_facts tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(None, description="Stock ticker symbol (e.g., 'AAPL')")
    name: str | None = Field(None, description="Company name (e.g., 'Apple Inc')")
    limit_concepts: int | None = Field(
        50, description="Max number of concepts per taxonomy (max: 100)"
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class GetCompanyFactsOutput(OutputBaseModel):
    """Output model for get_company_facts tool."""

    data: dict = Field(..., description="Financial facts and metrics from XBRL filings")


class GetCompanyConceptInput(BaseModel):
    """Input model for get_company_concept tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(None, description="Stock ticker symbol (e.g., 'AAPL')")
    name: str | None = Field(None, description="Company name (e.g., 'Apple Inc')")
    taxonomy: str = Field(
        ...,
        description=(
            "XBRL taxonomy namespace. Use 'us-gaap' for most US public companies "
            "(financial metrics). Use 'dei' for document/entity info (shares outstanding, "
            "document type). Use 'ifrs-full' for IFRS filers (rare for US-listed companies). "
            "Case-sensitive lowercase."
        ),
    )
    tag: str = Field(
        ...,
        description=(
            "XBRL concept tag name. Case-sensitive PascalCase. "
            "WARNING: Tag names are often not intuitive. Common mappings: "
            "Revenue -> 'Revenues' or 'RevenueFromContractWithCustomerExcludingAssessedTax', "
            "Net Income -> 'NetIncomeLoss', Total Assets -> 'Assets', "
            "Cash -> 'CashAndCashEquivalentsAtCarryingValue', "
            "Total Debt -> 'LongTermDebt' or 'DebtCurrent'. "
            "TIP: Use edgar_filings action='facts' first to discover exact tag names."
        ),
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class GetCompanyConceptOutput(OutputBaseModel):
    """Output model for get_company_concept tool."""

    data: dict = Field(..., description="All reported values for the concept across filings")


class GetFramesInput(BaseModel):
    """Input model for get_frames tool."""

    taxonomy: str = Field(
        ...,
        description=(
            "XBRL taxonomy namespace. Use 'us-gaap' for most US public companies "
            "(financial metrics). Use 'dei' for document/entity info (shares outstanding, "
            "document type). Use 'ifrs-full' for IFRS filers (rare for US-listed companies). "
            "Case-sensitive lowercase."
        ),
    )
    tag: str = Field(
        ...,
        description=(
            "XBRL concept tag name. Case-sensitive PascalCase. "
            "WARNING: Tag names are often not intuitive. Common mappings: "
            "Revenue -> 'Revenues' or 'RevenueFromContractWithCustomerExcludingAssessedTax', "
            "Net Income -> 'NetIncomeLoss', Total Assets -> 'Assets', "
            "Cash -> 'CashAndCashEquivalentsAtCarryingValue', "
            "Total Debt -> 'LongTermDebt' or 'DebtCurrent'. "
            "TIP: Use edgar_filings action='facts' first to discover exact tag names."
        ),
    )
    unit: str = Field(
        ...,
        description=(
            "Unit of measure for the financial data. Common values: "
            "'USD' (monetary amounts in US dollars), "
            "'shares' (share counts), "
            "'pure' (dimensionless ratios and percentages), "
            "'USD-per-shares' (per-share amounts like EPS)."
        ),
    )
    period: str = Field(
        ...,
        description=(
            "Reporting period in SEC frame format. "
            "DURATION periods (for income statement items like Revenue, NetIncome): "
            "Annual: 'CY2023' (calendar year 2023), "
            "Quarterly: 'CY2023Q1', 'CY2023Q2', 'CY2023Q3', 'CY2023Q4'. "
            "INSTANT periods (for balance sheet items like Assets, Cash, Debt - add 'I' suffix): "
            "Quarterly instant: 'CY2023Q4I' (point-in-time value as of quarter end). "
            "RULE: Use 'I' suffix for balance sheet items, no suffix for income/cash flow items."
        ),
    )


class GetFramesOutput(OutputBaseModel):
    """Output model for get_frames tool."""

    data: dict = Field(..., description="Aggregated data for all companies reporting this concept")


# Debt schedule models
class DebtInstrument(OutputBaseModel):
    """Individual debt instrument data."""

    instrument_name: str | None = Field(None, description="Name of debt instrument")
    current_portion: float | None = Field(
        None,
        description=(
            "Current portion of debt in USD, maturing within 12 months. "
            "Value in actual dollars (not scaled)."
        ),
    )
    noncurrent_portion: float | None = Field(
        None,
        description=(
            "Noncurrent portion of debt in USD, maturing after 12 months. "
            "Value in actual dollars (not scaled)."
        ),
    )
    maturity_date: str | None = Field(None, description="Maturity date (YYYY-MM-DD)")


class GetDebtScheduleInput(BaseModel):
    """Input model for get_debt_schedule tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(None, description="Stock ticker symbol (e.g., 'AAPL')")
    name: str | None = Field(None, description="Company name (e.g., 'Apple Inc')")
    filing_accession: str = Field(
        ...,
        description=(
            "SEC filing accession number in format 'XXXXXXXXXX-XX-XXXXXX' "
            "(e.g., '0000320193-24-000081'). "
            "IMPORTANT: This value must be obtained from edgar_filings action='submissions' "
            "response - it appears in the 'accession_number' field of each filing record. "
            "Do NOT fabricate this value."
        ),
        json_schema_extra={
            "x-populate-from": "get_company_submissions",
            "x-populate-field": "filings",
            "x-populate-value": "accession_number",
            "x-populate-display": "{form} ({filing_date}) {accession_number}",
            "x-populate-dependencies": {"ticker": "ticker"},
        },
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class GetDebtScheduleOutput(OutputBaseModel):
    """Output model for get_debt_schedule tool."""

    cik: str = Field(..., description="10-digit zero-padded CIK")
    filing_accession: str = Field(..., description="Filing accession number")
    report_date: str | None = Field(None, description="Report period end date (YYYY-MM-DD)")
    total_current_debt: float = Field(
        ...,
        description=(
            "Total current debt in USD, maturing within 12 months. "
            "Value in actual dollars (not scaled)."
        ),
    )
    total_noncurrent_debt: float = Field(
        ...,
        description=(
            "Total noncurrent debt in USD, maturing after 12 months. "
            "Value in actual dollars (not scaled)."
        ),
    )
    debt_instruments: list[DebtInstrument] | None = Field(
        None, description="Breakdown by debt instrument"
    )
    data_source: Literal["xbrl_api", "xbrl_parser", "html_scraper"] = Field(
        ..., description="Which retrieval tier succeeded"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        ...,
        description=(
            "Data extraction reliability indicator. "
            "'high': From structured XBRL API - highly reliable. "
            "'medium': From parsed XBRL files - generally accurate. "
            "'low': From HTML scraping - may have errors, consider manual verification "
            "by fetching the source document via edgar_documents."
        ),
    )


# HTML table extraction models
class ExtractHTMLTableInput(BaseModel):
    """Input model for extract_html_table tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(None, description="Stock ticker symbol (e.g., 'AAPL')")
    name: str | None = Field(None, description="Company name (e.g., 'Apple Inc')")
    filing_accession: str = Field(
        ...,
        description=(
            "SEC filing accession number in format 'XXXXXXXXXX-XX-XXXXXX' "
            "(e.g., '0000320193-24-000081'). "
            "IMPORTANT: This value must be obtained from edgar_filings action='submissions' "
            "response - it appears in the 'accession_number' field of each filing record. "
            "Do NOT fabricate this value."
        ),
        json_schema_extra={
            "x-populate-from": "get_company_submissions",
            "x-populate-field": "filings",
            "x-populate-value": "accession_number",
            "x-populate-display": "{form} ({filing_date}) {accession_number}",
            "x-populate-dependencies": {"ticker": "ticker"},
        },
    )
    table_keyword: str = Field(
        ...,
        description=(
            "Search keyword for finding tables in filing HTML. "
            "Case-insensitive substring match against table headers and surrounding text. "
            "Examples: 'stock option' (option activity), 'debt' or 'borrowings' (debt schedules), "
            "'revenue' (revenue breakdown), 'segment' (segment reporting), 'lease' (leases). "
            "TIP: Start with broader terms like 'debt' before trying specific phrases."
        ),
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class ExtractHTMLTableOutput(OutputBaseModel):
    """Output model for extract_html_table tool."""

    tables: list[dict] = Field(..., description="List of extracted tables matching the keyword")
    confidence: Literal["high", "medium", "low"] = Field(
        ...,
        description=(
            "Table extraction reliability indicator. "
            "'high': Table clearly identified with standard structure. "
            "'medium': Table found but structure may be non-standard. "
            "'low': No confident match found — the error field contains "
            "details about how many tables were scanned and why none matched "
            "well. Try a more specific keyword or use edgar_documents to read "
            "the filing directly."
        ),
    )
    error: str | None = Field(
        None,
        description=(
            "Error message if extraction failed. When confidence is 'low', "
            "this includes the best match reason and number of tables "
            "scanned to help refine the keyword."
        ),
    )


# Document retrieval models
class FilingDocumentInfo(OutputBaseModel):
    """Information about a single document in a filing."""

    filename: str = Field(..., description="Document filename (e.g., 'amcx-ex101.htm')")
    description: str | None = Field(None, description="Document description (e.g., 'EX-10.1')")
    url: str = Field(..., description="Full URL to the document")


class ListFilingDocumentsInput(BaseModel):
    """Input model for list_filing_documents tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(
        None,
        description="Stock ticker symbol (e.g., 'AAPL'). Provide ONE OF: cik, ticker, or name.",
    )
    name: str | None = Field(
        None,
        description="Company name (e.g., 'Apple Inc'). Provide ONE OF: cik, ticker, or name.",
    )
    filing_accession: str = Field(
        ...,
        description=(
            "SEC filing accession number in format 'XXXXXXXXXX-XX-XXXXXX' "
            "(e.g., '0000320193-24-000081'). "
            "IMPORTANT: This value must be obtained from edgar_filings action='submissions' "
            "response - it appears in the 'accession_number' field of each filing record. "
            "Do NOT fabricate this value."
        ),
        json_schema_extra={
            "x-populate-from": "get_company_submissions",
            "x-populate-field": "filings",
            "x-populate-value": "accession_number",
            "x-populate-display": "{form} ({filing_date}) {accession_number}",
            "x-populate-dependencies": {"ticker": "ticker"},
        },
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class ListFilingDocumentsOutput(OutputBaseModel):
    """Output model for list_filing_documents tool."""

    filing_accession: str = Field(..., description="Filing accession number")
    primary_document: str | None = Field(None, description="Primary document filename")
    documents: list[FilingDocumentInfo] = Field(
        ..., description="List of all documents in the filing"
    )
    total_documents: int = Field(..., description="Total number of documents")
    data_source: str | None = Field(
        None,
        description=(
            "Where the data was retrieved from. "
            "'offline_submissions': from local submissions JSON. "
            "'edgartools': from SEC EDGAR API via edgartools."
        ),
    )


class GetFilingDocumentInput(BaseModel):
    """Input model for get_filing_document tool."""

    cik: str | None = Field(
        None,
        description=(
            "SEC Central Index Key, 10-digit zero-padded string (e.g., '0000320193' for Apple). "
            "Provide ONE OF: cik, ticker, or name."
        ),
    )
    ticker: str | None = Field(
        None,
        description="Stock ticker symbol (e.g., 'AAPL'). Provide ONE OF: cik, ticker, or name.",
    )
    name: str | None = Field(
        None,
        description="Company name (e.g., 'Apple Inc'). Provide ONE OF: cik, ticker, or name.",
    )
    filing_accession: str = Field(
        ...,
        description=(
            "SEC filing accession number in format 'XXXXXXXXXX-XX-XXXXXX' "
            "(e.g., '0000320193-24-000081'). "
            "IMPORTANT: This value must be obtained from edgar_filings action='submissions' "
            "response - it appears in the 'accession_number' field of each filing record. "
            "Do NOT fabricate this value."
        ),
        json_schema_extra={
            "x-populate-from": "get_company_submissions",
            "x-populate-field": "filings",
            "x-populate-value": "accession_number",
            "x-populate-display": "{form} ({filing_date}) {accession_number}",
            "x-populate-dependencies": {"ticker": "ticker"},
        },
    )
    document: str = Field(
        "primary",
        description=(
            "Document to retrieve. Use 'primary' (default) for the main filing document. "
            "For exhibits, use exact filename from edgar_documents action='list' response. "
            "Common patterns: 'ex10-1.htm' (material contracts), 'ex21-1.htm' (subsidiaries), "
            "'ex99-1.htm' (press releases). Filenames vary by filer - always use list action first."
        ),
        json_schema_extra={
            "x-populate-from": "list_filing_documents",
            "x-populate-field": "documents",
            "x-populate-value": "filename",
            "x-populate-display": "{description} ({filename})",
            "x-populate-dependencies": {
                "filing_accession": "filing_accession",
                "ticker": "ticker",
            },
        },
    )

    @model_validator(mode="after")
    def check_at_least_one_identifier(self):
        """Validate that at least one of cik, ticker, or name is provided."""
        if not self.cik and not self.ticker and not self.name:
            raise ValueError("Must provide either cik, ticker, or name")
        return self


class GetFilingDocumentOutput(OutputBaseModel):
    """Output model for get_filing_document tool."""

    filing_accession: str = Field(..., description="Filing accession number")
    filename: str = Field(..., description="Document filename")
    text: str = Field(
        ...,
        description=(
            "Document text content. For documents exceeding 10,000 characters, "
            "this contains a truncated preview with a note pointing to file_path for full content."
        ),
    )
    url: str = Field(..., description="Full URL to the document")
    file_path: str | None = Field(
        None,
        description=(
            "Path to full document on local filesystem. "
            "Set when document exceeds 10,000 characters and was saved to disk."
        ),
    )
    data_source: str | None = Field(
        None,
        description=(
            "Where the data was retrieved from. "
            "'offline_filings': from local filing HTML files. "
            "'edgartools': from SEC EDGAR API via edgartools."
        ),
    )
