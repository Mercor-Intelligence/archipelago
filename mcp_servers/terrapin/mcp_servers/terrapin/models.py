"""Pydantic models for terrapin.

Define API specifications for bond data tools using Pydantic models.
"""

from mcp_schema import GeminiBaseModel
from pydantic import BaseModel, Field

# ============================================================================
# Structured Data Models (for response payloads)
# ============================================================================


class BondSearchResult(BaseModel):
    """A single bond from search results."""

    isin: str = Field(
        description="International Securities Identification Number (12-character alphanumeric code, e.g., 'XS2724510792')"
    )
    issuer_name: str | None = Field(
        None,
        description="Name of the bond issuer (e.g., 'Goldman Sachs Group Inc', 'Federal Republic of Germany')",
    )
    ticker: str | None = Field(
        None,
        description="Bond ticker symbol used by trading platforms (e.g., 'GS', 'DBR'). May be null for some bonds.",
    )
    coupon: float | None = Field(
        None,
        description="Annual coupon rate as a percentage (e.g., 4.5 means 4.5% per year). Null for zero-coupon bonds.",
    )
    maturity_date: str | None = Field(None, description="Maturity date (YYYY-MM-DD)")
    currency: str | None = Field(
        None, description="ISO 4217 3-letter currency code (e.g., 'USD', 'EUR', 'GBP')"
    )
    country: str | None = Field(
        None,
        description="Country of issue as ISO 3166-1 alpha-2 code (e.g., 'US', 'DE') or full name",
    )
    issue_rating_group: str | None = Field(
        None,
        description="Rating classification: 'investment_grade' (BBB- and above) or 'high_yield' (below BBB-). Null if unrated.",
    )


class BondReferenceResult(BaseModel):
    """Full reference data for a bond."""

    isin: str = Field(description="International Securities Identification Number")
    issuer_name: str | None = Field(None, description="Name of the bond issuer")
    ticker: str | None = Field(None, description="Bond ticker symbol")
    coupon: float | None = Field(
        None,
        description=(
            "Annual coupon rate as a percentage (e.g., 3.5 means 3.5% annual interest, NOT 0.035)"
        ),
    )
    coupon_frequency: int | None = Field(
        None,
        description=(
            "Number of coupon payments per year. "
            "1 = annual, 2 = semi-annual (most common), 4 = quarterly, 12 = monthly. "
            "Zero-coupon bonds may show 0 or null."
        ),
    )
    interest_type: str | None = Field(
        None,
        description=(
            "Interest payment structure. Common values: "
            "'Fixed' (constant coupon rate), 'Floating' (variable rate tied to benchmark), "
            "'Zero' (no periodic payments, sold at discount), "
            "'Step-Up' (rate increases on schedule)"
        ),
    )
    maturity_date: str | None = Field(None, description="Maturity date (YYYY-MM-DD)")
    issue_date: str | None = Field(
        None,
        description=(
            "Date bond was issued/sold to investors (YYYY-MM-DD). When the bond first traded."
        ),
    )
    currency: str | None = Field(None, description="Currency code (ISO 4217, e.g., USD, EUR, GBP)")
    country: str | None = Field(
        None, description="Country of issue (ISO 3166-1 alpha-2, e.g., US, DE, GB)"
    )
    issue_rating: str | None = Field(None, description="Issue credit rating (e.g., AAA, AA+, BBB-)")
    issue_rating_group: str | None = Field(
        None,
        description=(
            "Credit quality group: 'investment_grade' (BBB-/Baa3 or higher) "
            "or 'high_yield' (below BBB-/Baa3, also called 'junk bonds')"
        ),
    )
    issuer_rating: str | None = Field(
        None, description="Issuer credit rating (e.g., AAA, AA+, BBB-)"
    )
    issuer_rating_group: str | None = Field(
        None,
        description=("Issuer credit quality group: 'investment_grade' or 'high_yield'"),
    )
    asset_class: str | None = Field(
        None,
        description=(
            "Bond asset classification. Values: "
            "'Government' (sovereign debt), 'Corporate' (company-issued), "
            "'Agency' (government-sponsored entities), "
            "'Supranational' (international organizations)"
        ),
    )
    sector: str | None = Field(None, description="Industry sector of the issuer")
    lei: str | None = Field(
        None,
        description=(
            "Legal Entity Identifier - 20-character alphanumeric code identifying the issuer"
        ),
    )
    country_code: str | None = Field(
        None, description="Country code (ISO 3166-1 alpha-2, e.g., US, DE, GB)"
    )


class BondPricingResult(BaseModel):
    """Pricing data for a bond."""

    isin: str = Field(description="ISIN of the priced bond (12-character alphanumeric code)")
    pricing_date: str | None = Field(None, description="Pricing date (YYYY-MM-DD)")
    price: float | None = Field(
        None,
        description=(
            "Clean price as percentage of par value (face value). "
            "100.0 = par, >100 = premium, <100 = discount. "
            "Example: 98.5 means 98.5% of face value ($985 per $1,000 bond). "
            "Typical range: 80-120."
        ),
    )
    yield_to_maturity: float | None = Field(
        None,
        description=(
            "Yield to maturity (YTM) as annual percentage using bond-equivalent yield convention. "
            "Value of 4.25 means 4.25% annual yield assuming semi-annual compounding. "
            "NOT a decimal - use 4.25, not 0.0425."
        ),
    )
    duration: float | None = Field(
        None,
        description=(
            "Macaulay duration in years - weighted average time until cash flows are received"
        ),
    )
    modified_duration: float | None = Field(
        None,
        description=(
            "Modified duration - approximate percentage price change for a 1% change in yield. "
            "Example: 5.2 means price drops ~5.2% if yield rises 1 percentage point."
        ),
    )
    convexity: float | None = Field(
        None,
        description=(
            "Convexity measure - second-order price sensitivity to yield changes. "
            "Used with modified duration for more accurate price change estimates "
            "on large yield moves."
        ),
    )
    estimated_volume: float | None = Field(
        None,
        description=(
            "Estimated trading volume as face value in the bond's currency. "
            "Example: 1000000 for a USD bond means approximately $1M face value traded. "
            "This is an estimate based on market data, not actual reported volume."
        ),
    )


class CashflowEntry(BaseModel):
    """Single cash flow payment."""

    type: str | None = Field(
        None,
        description=(
            "Cash flow type. Expected values: "
            "'interest' (coupon payment), 'principal' (repayment at maturity or amortization), "
            "'interest_principal' (combined payment for some amortizing bonds). "
            "null if type cannot be determined."
        ),
    )
    date: str | None = Field(None, description="Payment date (YYYY-MM-DD)")
    amount: float | None = Field(
        None,
        description=(
            "Payment amount per $100 face value in the bond's currency. "
            "Example: 2.5 for a 5% semi-annual coupon = $2.50 per $100 every 6 months. "
            "Principal repayment at maturity is typically 100.0."
        ),
    )


class BondCashflowResult(BaseModel):
    """Cash flow schedule for a bond."""

    isin: str = Field(description="ISIN of the bond (12-character alphanumeric code)")
    cashflows: list[CashflowEntry] | None = Field(
        None,
        description="Chronological list of scheduled cash flow payments (interest and principal). Null if cashflow data unavailable.",
    )


class InflationFactorResult(BaseModel):
    """Inflation factor data point."""

    date: str = Field(description="Factor date (YYYY-MM-DD)")
    factor: float | None = Field(
        None,
        description="Inflation index ratio for adjusting principal. Value of 1.05 means 5% cumulative inflation since base date.",
    )
    type: str | None = Field(
        None,
        description="Inflation index type: 'BLS' (US Bureau of Labor Statistics CPI), 'HICP' (EU Harmonized Index), etc.",
    )


class MuniBondSearchResult(BaseModel):
    """A single municipal bond from search results."""

    isin: str = Field(description="International Securities Identification Number")
    issuer_name: str | None = Field(None, description="Name of the bond issuer")
    coupon: float | None = Field(
        None,
        description=(
            "Annual coupon rate as a percentage (e.g., 3.5 means 3.5% annual interest, NOT 0.035)"
        ),
    )
    maturity_date: str | None = Field(None, description="Maturity date (YYYY-MM-DD)")
    state: str | None = Field(
        None, description="US state code (ANSI 2-letter format, e.g., CA, NY, TX)"
    )
    sector: str | None = Field(
        None,
        description=(
            "Municipal bond sector. Common values: 'education', 'healthcare', "
            "'utilities', 'water_sewer', 'transportation', 'airport', "
            "'toll_road', 'housing', 'general_obligation'"
        ),
    )
    source_of_repayment: str | None = Field(
        None,
        description=(
            "Bond repayment source. Values: "
            "'Revenue' (backed by project income), 'General Obligation' (backed by taxing power), "
            "'Double Barrel' (backed by both revenue AND taxing power)"
        ),
    )
    is_insured: bool | None = Field(
        None, description="Whether bond is insured by a third-party guarantor"
    )


class MuniReferenceResult(BaseModel):
    """Full reference data for a municipal bond."""

    isin: str = Field(description="International Securities Identification Number")
    issuer_name: str | None = Field(None, description="Name of the bond issuer")
    coupon: float | None = Field(
        None,
        description=(
            "Annual coupon rate as a percentage (e.g., 3.5 means 3.5% annual interest, NOT 0.035)"
        ),
    )
    coupon_type: str | None = Field(
        None,
        description="Coupon type (e.g., 'Fixed', 'Variable', 'Zero')",
    )
    coupon_frequency: str | None = Field(
        None,
        description=(
            "Coupon payment frequency as text. "
            "Common values: 'Semi-Annual' (most common), 'Annual', 'Quarterly', 'Monthly'."
        ),
    )
    maturity_date: str | None = Field(None, description="Maturity date (YYYY-MM-DD)")
    issue_date: str | None = Field(
        None,
        description=(
            "Date bond was issued/sold to investors (YYYY-MM-DD). When the bond first traded."
        ),
    )
    dated_date: str | None = Field(
        None,
        description=(
            "Date from which interest begins accruing (YYYY-MM-DD). "
            "May differ from issue_date for pre-funded bonds. "
            "Used to calculate accrued interest for trades between coupon dates."
        ),
    )
    first_coupon_date: str | None = Field(
        None,
        description=(
            "Date of first coupon payment (YYYY-MM-DD). "
            "May be a 'short' first coupon (less than full period) or "
            "'long' first coupon (more than full period) depending on dated_date."
        ),
    )
    state: str | None = Field(
        None, description="US state code (ANSI 2-letter format, e.g., CA, NY, TX)"
    )
    sector: str | None = Field(
        None,
        description=(
            "Municipal bond sector. Common values: 'education', 'healthcare', "
            "'utilities', 'water_sewer', 'transportation', 'airport', "
            "'toll_road', 'housing', 'general_obligation'"
        ),
    )
    purpose: str | None = Field(None, description="Bond purpose - specific use of bond proceeds")
    source_of_repayment: str | None = Field(
        None,
        description=(
            "Bond repayment source. Values: "
            "'Revenue' (backed by project income), 'General Obligation' (backed by taxing power), "
            "'Double Barrel' (backed by both revenue AND taxing power)"
        ),
    )
    is_insured: bool | None = Field(
        None, description="Whether bond is insured by a third-party guarantor"
    )
    insurer: str | None = Field(None, description="Name of the bond insurance company, if insured")
    credit_enhancement: str | None = Field(
        None,
        description=(
            "Credit support beyond issuer's own credit. Values: "
            "'Bond Insurance' (third-party guarantees payment), 'Letter of Credit' (bank backing), "
            "'State Aid Intercept' (state can intercept payments if issuer defaults), "
            "'Reserve Fund' (cash set aside for payments). null/None = no enhancement."
        ),
    )
    tax_status: str | None = Field(
        None,
        description=(
            "Federal tax treatment of bond interest. Values: "
            "'Tax-Exempt' (interest exempt from federal income tax - most municipal bonds), "
            "'Taxable' (interest subject to federal income tax), "
            "'AMT' (tax-exempt but subject to Alternative Minimum Tax). "
            "Note: State/local tax treatment varies by investor's residence."
        ),
    )
    callable: bool | None = Field(
        None, description="Whether bond can be redeemed early by the issuer"
    )
    call_date: str | None = Field(None, description="Earliest call date (YYYY-MM-DD)")
    call_price: float | None = Field(
        None,
        description=(
            "Call price as percentage of par value. "
            "Example: 102.0 means issuer can redeem at 102% of face value. "
            "100.0 = callable at par. Values above 100 represent call premiums."
        ),
    )
    underwriters: list[str] | None = Field(
        None, description="List of underwriters who marketed the bond"
    )


class MuniPricingResult(BaseModel):
    """Pricing data for a municipal bond."""

    isin: str = Field(description="ISIN (12-character code)")
    trade_date: str | None = Field(None, description="Trade date (YYYY-MM-DD)")
    price: float | None = Field(
        None,
        description=(
            "Clean price as percentage of par value (face value). "
            "100.0 = par, >100 = premium, <100 = discount. "
            "Example: 98.5 means 98.5% of face value ($985 per $1,000 bond). "
            "Typical range: 80-120."
        ),
    )
    yield_to_maturity: float | None = Field(
        None,
        description=(
            "Yield to maturity (YTM) as annual percentage using bond-equivalent yield convention. "
            "Value of 4.25 means 4.25% annual yield assuming semi-annual compounding. "
            "NOT a decimal - use 4.25, not 0.0425."
        ),
    )
    yield_to_call: float | None = Field(
        None,
        description=(
            "Yield to call as annual percentage - yield assuming bond is called "
            "at earliest call date. Only relevant for callable bonds. "
            "Value of 4.25 means 4.25% annual yield. NOT a decimal - use 4.25, not 0.0425."
        ),
    )
    trade_amount: float | None = Field(
        None,
        description=(
            "Trade amount as face value in USD. "
            "Example: 100000 means $100,000 face value was traded. "
            "Market value = trade_amount * (price / 100)."
        ),
    )
    trade_type: str | None = Field(
        None,
        description=(
            "Type of trade transaction. Common values: "
            "'Customer Buy' (dealer sells to customer), "
            "'Customer Sell' (dealer buys from customer), "
            "'Inter-Dealer' (trade between two dealers). "
            "Useful for understanding market dynamics and price discovery."
        ),
    )
    settlement_date: str | None = Field(None, description="Settlement date (YYYY-MM-DD)")


class MuniCashflowResult(BaseModel):
    """Cash flow schedule for a municipal bond."""

    isin: str = Field(description="ISIN of the bond (12-character code)")
    cashflows: list[CashflowEntry] | None = Field(
        None,
        description="Chronological list of scheduled payments. Null if cashflow data unavailable for this bond.",
    )


class YieldConventions(BaseModel):
    """Yield values under different day count conventions."""

    continuous: float | None = Field(
        None,
        description="Continuously compounded annual yield as percentage (e.g., 3.45). Used in derivatives pricing.",
    )
    money_market: float | None = Field(
        None,
        description="Money market yield using actual/360 day count, as percentage (e.g., 3.42)",
    )
    semi_annual: float | None = Field(
        None,
        description="Semi-annual bond equivalent yield as percentage (e.g., 3.50). Standard US bond convention.",
    )


class MuniYieldResult(BaseModel):
    """Yield calculation result for a municipal bond."""

    isin: str = Field(description="ISIN of the calculated bond (12-character code)")
    price: float | None = Field(
        None, description="Input price as percentage of par (echoed from request)"
    )
    settlement_date: str | None = Field(
        None, description="Settlement date used for calculation (YYYY-MM-DD)"
    )
    yield_to_maturity: YieldConventions | None = Field(
        None, description="Yield to maturity calculated under various day count conventions"
    )
    yield_to_call: YieldConventions | None = Field(
        None, description="Yield to call under various conventions. Null if bond is not callable."
    )
    error: str | None = Field(None, description="Error message if calculation failed")


# ============================================================================
# Government and Corporate Bonds - Request Models
# ============================================================================


class SearchBondsRequest(GeminiBaseModel):
    """Input specification for search_bonds."""

    countries: list[str] | None = Field(
        default=None,
        description="Countries to filter by (ISO 3166-1 alpha-2 codes, e.g., US, DE, GB)",
        examples=[["US", "DE", "GB"]],
    )
    coupon_min: float | None = Field(
        default=None,
        description=(
            "Minimum coupon rate as a percentage (e.g., 3.0 means 3.0% annual coupon). "
            "NOT a decimal - use 3.0, not 0.03."
        ),
        examples=[0.0, 3.0, 5.25],
    )
    coupon_max: float | None = Field(
        default=None,
        description=(
            "Maximum coupon rate as a percentage (e.g., 5.0 means 5.0% annual coupon). "
            "NOT a decimal - use 5.0, not 0.05."
        ),
        examples=[5.0, 7.5, 10.0],
    )
    maturity_date_min: str | None = Field(
        default=None,
        description="Minimum maturity date (YYYY-MM-DD format)",
        examples=["2025-01-01"],
    )
    maturity_date_max: str | None = Field(
        default=None,
        description="Maximum maturity date (YYYY-MM-DD format)",
        examples=["2030-12-31"],
    )
    currencies: list[str] | None = Field(
        default=None,
        description="Currencies to filter by (ISO 4217 3-letter codes, e.g., USD, EUR, GBP)",
        examples=[["USD", "EUR", "GBP"]],
    )
    issue_rating_group: str | None = Field(
        default=None,
        description=(
            "Credit quality filter. Only two valid values: "
            "'investment_grade' (bonds rated BBB-/Baa3 or higher - lower default risk), "
            "'high_yield' (bonds below BBB-/Baa3, also called 'junk bonds' - higher risk)."
            "Omit to return bonds of all ratings."
        ),
        examples=["investment_grade", "high_yield"],
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of results to return (default: 100, max: 1000). If omitted, returns up to 100 results.",
        examples=[50],
    )


class SearchBondsResponse(BaseModel):
    """Output specification for search_bonds."""

    bonds: list[BondSearchResult] = Field(
        default_factory=list,
        description="List of bonds matching the search criteria. Empty list if no matches found.",
    )


class BondReferenceDataRequest(GeminiBaseModel):
    """Input specification for get_bond_reference_data."""

    isins: list[str] = Field(
        description="List of bond ISINs (12-character alphanumeric codes, e.g., ['XS2724510792', 'US912828Z490']). Maximum batch size: 100 ISINs per request.",
        examples=[["XS2724510792", "US912828Z490"]],
    )


class BondReferenceDataResponse(BaseModel):
    """Output specification for get_bond_reference_data."""

    reference: list[BondReferenceResult] = Field(
        default_factory=list,
        description="List of bond reference data objects. May contain fewer items than requested if some ISINs are not found.",
    )
    message: str | None = Field(
        default=None,
        description=(
            "Status message explaining results. Present when: "
            "no data found (check if ISINs are valid government/corporate bonds, not municipal), "
            "partial results (some ISINs found, others missing or invalid). "
            "null when all requested data returned successfully. "
            "Check 'requested_isins' field for list of ISINs that had no data."
        ),
    )
    requested_isins: list[str] | None = Field(
        default=None,
        description="ISINs from the request that were not found in the database. Null if all ISINs were found.",
    )


class BondPricingHistoryRequest(GeminiBaseModel):
    """Input specification for get_bond_pricing_history."""

    isin: str = Field(
        description=(
            "Single bond ISIN (12-character code). "
            "Historical pricing is retrieved one bond at a time due to data volume. "
            "Format: 2-letter country code + 9 alphanumeric + 1 check digit. "
            "Example: 'US912828Z490' for a US Treasury."
        ),
        examples=["XS1610682764", "US912828Z490"],
    )
    start_date: str = Field(
        description="Start date for pricing history (YYYY-MM-DD, e.g., '2024-01-01'). Must be before end_date. Data available from 2020 onwards.",
        examples=["2024-01-01"],
    )
    end_date: str = Field(
        description="End date for pricing history (YYYY-MM-DD, e.g., '2024-12-31'). Must be after start_date. Cannot be future date.",
        examples=["2024-12-31"],
    )


class BondPricingHistoryResponse(BaseModel):
    """Output specification for get_bond_pricing_history."""

    pricing: list[BondPricingResult] = Field(
        default_factory=list,
        description="List of daily pricing data points, ordered chronologically (oldest first). Empty if no data in date range.",
    )
    message: str | None = Field(
        default=None,
        description="Informational message about the results (e.g., why no data was found)",
    )


class BondPricingLatestRequest(GeminiBaseModel):
    """Input specification for get_bond_pricing_latest."""

    isins: list[str] = Field(
        description=(
            "List of International Securities Identification Numbers (ISINs). "
            "Format: 12 alphanumeric characters - first 2 are ISO country code. "
            "Max 100 ISINs per request. Invalid ISINs return no data (not an error)."
        ),
        examples=[["XS1610682764", "US912828Z490"]],
    )
    as_of_date: str | None = Field(
        default=None,
        description=(
            "Retrieve pricing as of a specific date (YYYY-MM-DD format). "
            "Returns the most recent pricing ON OR BEFORE this date. "
            "If omitted, returns the most recent available pricing (may lag 1-2 business days). "
            "Weekend/holiday dates use the prior business day's pricing. "
            "Bonds with no pricing on or before this date are omitted from results."
        ),
        examples=["2024-11-01"],
    )


class BondPricingLatestResponse(BaseModel):
    """Output specification for get_bond_pricing_latest."""

    pricing: list[BondPricingResult] = Field(
        default_factory=list,
        description="List of latest pricing data for requested bonds. May contain fewer items if pricing unavailable for some ISINs.",
    )
    message: str | None = Field(
        default=None,
        description="Status message. Present when some requested ISINs had invalid format and were skipped.",
    )


class BondCashflowsRequest(GeminiBaseModel):
    """Input specification for get_bond_cashflows."""

    isins: list[str] = Field(
        description="List of bond ISINs (12-character codes, e.g., ['XS1610682764']). Maximum 50 ISINs per request.",
        examples=[["XS1610682764"]],
    )


class BondCashflowsResponse(BaseModel):
    """Output specification for get_bond_cashflows."""

    cashflows: list[BondCashflowResult] = Field(
        default_factory=list,
        description="List of cash flow schedules, one per requested ISIN. Each contains the ISIN and its scheduled payments.",
    )


class InflationFactorsRequest(GeminiBaseModel):
    """Input specification for get_inflation_factors."""

    country: str = Field(
        description="Country code (ISO 3166-1 alpha-2, e.g., US, DE, GB)",
        examples=["US", "DE", "GB"],
    )
    start_date: str | None = Field(
        default=None,
        description="Start date for inflation factors (YYYY-MM-DD, e.g., '2023-01-01'). If omitted, returns last 12 months.",
        examples=["2023-01-01"],
    )
    end_date: str | None = Field(
        default=None,
        description="End date for inflation factors (YYYY-MM-DD, e.g., '2024-01-01'). If omitted, returns up to current date.",
        examples=["2024-01-01"],
    )


class InflationFactorsResponse(BaseModel):
    """Output specification for get_inflation_factors."""

    factors: list[InflationFactorResult] = Field(
        default_factory=list,
        description="List of inflation factors ordered chronologically (oldest first). Used for adjusting inflation-linked bond values.",
    )


# ============================================================================
# US Municipal Bonds - Request Models
# ============================================================================


class SearchMunicipalBondsRequest(GeminiBaseModel):
    """Input specification for search_municipal_bonds."""

    states: list[str] | None = Field(
        default=None,
        description="US state codes (ANSI 2-letter format, e.g., CA, NY, TX)",
        examples=[["CA", "NY", "TX"]],
    )
    coupon_min: float | None = Field(
        default=None,
        description=(
            "Minimum coupon rate as a percentage (e.g., 2.0 means 2.0% annual coupon). "
            "NOT a decimal - use 2.0, not 0.02."
        ),
        examples=[0.0, 2.0, 3.5],
    )
    coupon_max: float | None = Field(
        default=None,
        description=(
            "Maximum coupon rate as a percentage (e.g., 4.0 means 4.0% annual coupon). "
            "NOT a decimal - use 4.0, not 0.04."
        ),
        examples=[4.0, 5.0, 6.0],
    )
    maturity_date_min: str | None = Field(
        default=None,
        description="Minimum maturity date (YYYY-MM-DD format)",
        examples=["2025-01-01"],
    )
    maturity_date_max: str | None = Field(
        default=None,
        description="Maximum maturity date (YYYY-MM-DD format)",
        examples=["2030-12-31"],
    )
    sectors: list[str] | None = Field(
        default=None,
        description=(
            "Filter by municipal bond sector(s). Case-insensitive. Known values: "
            "'education' (school districts, universities), "
            "'healthcare' (hospitals, health systems), "
            "'utilities' (electric, gas utilities), "
            "'water_sewer' (water and wastewater systems), "
            "'transportation' (transit authorities), 'airport' (airport authorities), "
            "'toll_road' (toll roads and bridges), 'housing' (housing authorities), "
            "'general_obligation' (general GO bonds). "
            "Multiple sectors: ['education', 'healthcare']"
        ),
        examples=[["education"], ["healthcare", "utilities"]],
    )
    sources_of_repayment: list[str] | None = Field(
        default=None,
        description=(
            "Filter by bond repayment source. Valid values (case-sensitive): "
            "'Revenue' (backed by specific project income - higher risk, higher yield), "
            "'General Obligation' (backed by issuer's full taxing power - lower risk), "
            "'Double Barrel' (backed by BOTH revenue AND taxing power - most secure)."
        ),
        examples=[["Revenue"], ["General Obligation", "Double Barrel"]],
    )
    is_insured: bool | None = Field(
        default=None,
        description="Filter by bond insurance status. True=only insured bonds, False=only uninsured, null/omitted=all bonds.",
        examples=[True],
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of results (default: 100, max: 1000)",
        examples=[25],
    )


class SearchMunicipalBondsResponse(BaseModel):
    """Output specification for search_municipal_bonds."""

    bonds: list[MuniBondSearchResult] = Field(
        default_factory=list,
        description="List of municipal bonds matching search criteria. Empty list if no matches.",
    )


class MuniReferenceDataRequest(GeminiBaseModel):
    """Input specification for get_muni_reference_data."""

    isins: list[str] = Field(
        description="List of municipal bond ISINs (12-character codes, e.g., ['US12345ABC67']). Maximum 100 ISINs per request.",
        examples=[["US12345ABC67"]],
    )


class MuniReferenceDataResponse(BaseModel):
    """Output specification for get_muni_reference_data."""

    reference: list[MuniReferenceResult] = Field(
        default_factory=list,
        description="List of reference data for requested bonds. May be partial if some ISINs not found.",
    )


class MuniPricingLatestRequest(GeminiBaseModel):
    """Input specification for get_muni_pricing_latest."""

    isins: list[str] = Field(
        description=(
            "List of municipal bond ISINs. "
            "Format: 12 alphanumeric characters starting with 'US'. "
            "Max 100 ISINs per request. Invalid ISINs return no data (not an error)."
        ),
        examples=[["US12345ABC67"]],
    )
    as_of_date: str | None = Field(
        default=None,
        description=(
            "Retrieve pricing as of a specific date (YYYY-MM-DD format). "
            "Returns the most recent pricing ON OR BEFORE this date. "
            "If omitted, returns the most recent available pricing (may lag 1-2 business days). "
            "Weekend/holiday dates use the prior business day's pricing."
        ),
        examples=["2024-11-01"],
    )


class MuniPricingLatestResponse(BaseModel):
    """Output specification for get_muni_pricing_latest."""

    pricing: list[MuniPricingResult] = Field(
        default_factory=list,
        description="Latest pricing for each requested ISIN. May be partial if some bonds have no recent trades.",
    )


class MuniPricingHistoryRequest(GeminiBaseModel):
    """Input specification for get_muni_pricing_history."""

    isin: str = Field(
        description=(
            "Single municipal bond ISIN (12-character code starting with 'US'). "
            "Historical pricing is retrieved one bond at a time due to data volume."
        ),
        examples=["US12345ABC67"],
    )
    start_date: str = Field(
        description="Start date (YYYY-MM-DD). Must be before end_date. Data available from 2020.",
        examples=["2024-01-01"],
    )
    end_date: str = Field(
        description="End date (YYYY-MM-DD). Must be after start_date. Cannot be future date.",
        examples=["2024-12-31"],
    )


class MuniPricingHistoryResponse(BaseModel):
    """Output specification for get_muni_pricing_history."""

    pricing: list[MuniPricingResult] = Field(
        default_factory=list,
        description="Historical trade data ordered by trade_date (oldest first). Note: munis trade infrequently, gaps are normal.",
    )


class MuniPricingDailyBulkRequest(GeminiBaseModel):
    """Input specification for get_muni_pricing_daily_bulk."""

    trade_date: str = Field(
        description="Trade date to retrieve all muni trades (YYYY-MM-DD). Must be a past date. Returns all trades on this date.",
        examples=["2024-11-01"],
    )


class MuniPricingDailyBulkResponse(BaseModel):
    """Output specification for get_muni_pricing_daily_bulk."""

    pricing: list[MuniPricingResult] = Field(
        default_factory=list,
        description="All municipal bond trades on the specified date. Typically 50,000-200,000 trades per day. Empty on weekends/holidays.",
    )


class MuniYieldFromPriceRequest(GeminiBaseModel):
    """Input specification for calculate_muni_yield_from_price."""

    isin: str = Field(
        description=(
            "Municipal bond ISIN (12-character code starting with 'US'). "
            "Bond reference data must exist for yield calculation to succeed."
        ),
        examples=["US12345ABC67"],
    )
    price: float = Field(
        description=(
            "Clean price as percentage of par value (face value). "
            "100.0 = par, >100 = premium, <100 = discount. "
            "Example: 102.5 means 102.5% of face value. Typical range: 80-120."
        ),
        examples=[98.5, 100.0, 102.5],
    )
    settlement_date: str = Field(
        description=(
            "Settlement date for yield calculation (YYYY-MM-DD format). "
            "Must be a valid business day."
        ),
        examples=["2024-11-15"],
    )


class MuniYieldFromPriceResponse(BaseModel):
    """Output specification for calculate_muni_yield_from_price."""

    result: MuniYieldResult | None = Field(
        None,
        description="Yield calculation result, or null if calculation failed (check error field for details).",
    )


class MuniCashflowsRequest(GeminiBaseModel):
    """Input specification for get_muni_cashflows."""

    isins: list[str] = Field(
        description="List of muni ISINs (12-character codes). Maximum 50 ISINs per request.",
        examples=[["US12345ABC67"]],
    )


class MuniCashflowsResponse(BaseModel):
    """Output specification for get_muni_cashflows."""

    cashflows: list[MuniCashflowResult] = Field(
        default_factory=list,
        description="Cash flow schedules, one entry per requested ISIN containing its payment schedule.",
    )
