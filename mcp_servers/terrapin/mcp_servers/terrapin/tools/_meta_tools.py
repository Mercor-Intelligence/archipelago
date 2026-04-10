"""
Terrapin Meta-Tools for LLM Context Optimization.

Consolidates bond tools into domain-based meta-tools.
Each meta-tool supports action="help" for discovery.

Meta-tools:
- terrapin_bonds: Government and corporate bond operations
- terrapin_schema: Tool introspection
"""

import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# Ensure we can import from the server directory (required for direct script execution)
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_schema import GeminiBaseModel

# Import existing tool functions to delegate to
from tools._bonds import (  # noqa: E402
    BondCashflowsRequest,
    BondPricingHistoryRequest,
    BondPricingLatestRequest,
    BondReferenceDataRequest,
    InflationFactorsRequest,
    SearchBondsRequest,
    get_bond_cashflows,
    get_bond_pricing_history,
    get_bond_pricing_latest,
    get_bond_reference_data,
    get_inflation_factors,
    search_bonds,
)

# =============================================================================
# Output Models
# =============================================================================


class HelpResponse(BaseModel):
    """Response for action=help requests."""

    tool_name: str
    description: str
    actions: dict[str, dict[str, Any]] = Field(
        description="Actions with required_params, optional_params, description"
    )


# =============================================================================
# Input Models for Meta-Tools
# =============================================================================


class BondsInput(GeminiBaseModel):
    """Input for terrapin_bonds meta-tool."""

    action: Literal[
        "help",
        "search",
        "reference",
        "pricing_latest",
        "pricing_history",
        "cashflows",
        "inflation_factors",
    ] = Field(
        description=(
            "Operation to perform. Each action requires different parameters:\n"
            "- 'help': No params. Returns documentation for all actions.\n"
            "- 'search': Find bonds. Optional: countries, coupon_min/max, maturity dates, currencies, issue_rating_group, limit.\n"
            "- 'reference': Get bond details. REQUIRED: isins (list of ISINs).\n"
            "- 'pricing_latest': Get current prices. REQUIRED: isins. Optional: as_of_date.\n"
            "- 'pricing_history': Get price history. REQUIRED: isin (single!), start_date, end_date.\n"
            "- 'cashflows': Get payment schedule. REQUIRED: isins (list).\n"
            "- 'inflation_factors': Get inflation data. REQUIRED: country. Optional: start_date, end_date."
        )
    )
    # Search parameters
    countries: list[str] | None = Field(
        None, description="Filter by country codes (ISO 3166-1 alpha-2, e.g., US, DE, GB)"
    )
    coupon_min: float | None = Field(
        None,
        description="Minimum coupon rate as percentage (e.g., 3.0 means 3.0%, NOT 0.03)",
    )
    coupon_max: float | None = Field(
        None,
        description="Maximum coupon rate as percentage (e.g., 5.0 means 5.0%, NOT 0.05)",
    )
    maturity_date_min: str | None = Field(
        None, description="Earliest maturity date (YYYY-MM-DD format)"
    )
    maturity_date_max: str | None = Field(
        None, description="Latest maturity date (YYYY-MM-DD format)"
    )
    currencies: list[str] | None = Field(
        None, description="Filter by currency codes (ISO 4217, e.g., USD, EUR, GBP)"
    )
    issue_rating_group: str | None = Field(
        None,
        description=(
            "Rating group filter: 'investment_grade' (BBB-/Baa3 or higher) "
            "or 'high_yield' (below BBB-/Baa3). Omit to return all ratings."
        ),
    )
    limit: int | None = Field(
        100,
        description="Max results to return. Default: 100, Maximum: 1000. Results not in guaranteed order.",
    )
    # Reference/Pricing/Cashflows parameters
    isins: list[str] | None = Field(
        None,
        description=(
            "List of ISINs for batch operations: 'reference', 'pricing_latest', 'cashflows'. "
            "For 'pricing_history', use the singular isin parameter instead. "
            "Max 100 ISINs per request. Example: ['US912828Z490', 'XS2724510792']"
        ),
    )
    isin: str | None = Field(
        None,
        description=(
            "Single ISIN for 'pricing_history' action only. "
            "Historical pricing is retrieved one bond at a time due to data volume. "
            "For batch operations ('reference', 'pricing_latest', 'cashflows'), use the plural isins parameter instead. "
            "Example: 'US912828Z490'"
        ),
    )
    start_date: str | None = Field(
        None,
        description=(
            "Range start (YYYY-MM-DD format). REQUIRED for 'pricing_history' and optional for 'inflation_factors'."
        ),
    )
    end_date: str | None = Field(
        None,
        description=(
            "Range end (YYYY-MM-DD format). REQUIRED for 'pricing_history' and optional for 'inflation_factors'."
        ),
    )
    as_of_date: str | None = Field(
        None,
        description=(
            "Get pricing as of specific date (YYYY-MM-DD format). "
            "For 'pricing_latest' action only. If omitted, returns most recent available pricing."
        ),
    )
    # Inflation factors
    country: str | None = Field(
        None,
        description="ISO country code for inflation factors (e.g., 'US'). REQUIRED for 'inflation_factors' action.",
    )


class SchemaInput(GeminiBaseModel):
    """Input for terrapin_schema meta-tool."""

    tool_name: str | None = Field(
        None,
        description="Tool name to get schema for (e.g., 'terrapin_bonds'). If null/omitted, returns list of available tools.",
    )


# =============================================================================
# Meta-Tool Functions
# =============================================================================


async def terrapin_bonds(request: BondsInput) -> dict:
    """
    Access government and corporate bond data from Terrapin Finance API.

    This tool provides 6 actions for working with non-municipal bonds:
    - 'help': List all available actions and their parameters
    - 'search': Find bonds by country, coupon, maturity, currency, or rating
    - 'reference': Get detailed bond information (issuer, coupon frequency, ratings, LEI)
    - 'pricing_latest': Get most recent price, yield, duration, and volume
    - 'pricing_history': Get historical daily pricing for a date range (single ISIN only)
    - 'cashflows': Get scheduled interest and principal payments
    - 'inflation_factors': Get inflation index values for TIPS calculations

    Returns a dictionary with action-specific data. Use action='help' first
    to see required and optional parameters for each action.

    Returns:
        dict: Response structure varies by action:
        - help: {"tool_name": str, "description": str, "actions": {...}}
        - search: {"bonds": [{"isin": str, "issuer_name": str, "coupon": float, ...}]}
        - reference: {"reference": [...], "message": str|null, "requested_isins": list|null}
        - pricing_latest: {"pricing": [{"isin": str, "price": float, "yield_to_maturity": float, ...}]}
        - pricing_history: {"pricing": [...], "message": str|null}
        - cashflows: {"cashflows": [{"isin": str, "cashflows": [{"type": str, "date": str, "amount": float}]}]}
        - inflation_factors: {"factors": [{"date": str, "factor": float, "type": str}]}
        - On missing required params: {"error": "description of what's missing"}
    """
    if request.action == "help":
        return HelpResponse(
            tool_name="terrapin_bonds",
            description="Government and corporate bond search, reference, pricing, and cashflows.",
            actions={
                "search": {
                    "description": "Search bonds by criteria",
                    "required_params": [],
                    "optional_params": [
                        "countries",
                        "coupon_min",
                        "coupon_max",
                        "maturity_date_min",
                        "maturity_date_max",
                        "currencies",
                        "issue_rating_group",
                        "limit",
                    ],
                },
                "reference": {
                    "description": "Get bond reference data",
                    "required_params": ["isins"],
                    "optional_params": [],
                },
                "pricing_latest": {
                    "description": "Get latest bond pricing",
                    "required_params": ["isins"],
                    "optional_params": ["as_of_date"],
                },
                "pricing_history": {
                    "description": "Get historical bond pricing",
                    "required_params": ["isin", "start_date", "end_date"],
                    "optional_params": [],
                },
                "cashflows": {
                    "description": "Get bond cash flow schedules",
                    "required_params": ["isins"],
                    "optional_params": [],
                },
                "inflation_factors": {
                    "description": "Get inflation factors by country",
                    "required_params": ["country"],
                    "optional_params": ["start_date", "end_date"],
                },
            },
        ).model_dump()

    if request.action == "search":
        result = await search_bonds(
            SearchBondsRequest(
                countries=request.countries,
                coupon_min=request.coupon_min,
                coupon_max=request.coupon_max,
                maturity_date_min=request.maturity_date_min,
                maturity_date_max=request.maturity_date_max,
                currencies=request.currencies,
                issue_rating_group=request.issue_rating_group,
                limit=request.limit,
            )
        )
        return result.model_dump()
    elif request.action == "reference":
        if not request.isins:
            return {"error": "isins required for reference action"}
        result = await get_bond_reference_data(BondReferenceDataRequest(isins=request.isins))
        return result.model_dump()
    elif request.action == "pricing_latest":
        if not request.isins:
            return {"error": "isins required for pricing_latest action"}
        result = await get_bond_pricing_latest(
            BondPricingLatestRequest(isins=request.isins, as_of_date=request.as_of_date)
        )
        return result.model_dump()
    elif request.action == "pricing_history":
        if not request.isin or not request.start_date or not request.end_date:
            return {"error": "isin, start_date, end_date required for pricing_history action"}
        result = await get_bond_pricing_history(
            BondPricingHistoryRequest(
                isin=request.isin, start_date=request.start_date, end_date=request.end_date
            )
        )
        return result.model_dump()
    elif request.action == "cashflows":
        if not request.isins:
            return {"error": "isins required for cashflows action"}
        result = await get_bond_cashflows(BondCashflowsRequest(isins=request.isins))
        return result.model_dump()
    elif request.action == "inflation_factors":
        if not request.country:
            return {"error": "country required for inflation_factors action"}
        result = await get_inflation_factors(
            InflationFactorsRequest(
                country=request.country, start_date=request.start_date, end_date=request.end_date
            )
        )
        return result.model_dump()

    return {"error": f"Unknown action: {request.action}"}


async def terrapin_schema(request: SchemaInput) -> dict:
    """
    Get the JSON Schema for Terrapin tool input parameters.

    Use this to understand the exact structure and types expected by each tool.
    Call with tool_name=None to list available tools, then call with a specific
    tool_name to get its full parameter schema.

    Useful for validation or programmatic discovery of tool capabilities.

    Args:
        request: Contains tool_name - name of tool to get schema for, or None to list all tools

    Returns:
        If tool_name is None: {'available_tools': [...], 'usage': '...'}
        If tool_name provided: Full JSON Schema for that tool's input parameters
        If tool_name invalid: {'error': '...', 'available_tools': [...]}
    """
    tools = {
        "terrapin_bonds": BondsInput,
    }

    if request.tool_name is None:
        return {
            "available_tools": list(tools.keys()),
            "usage": "Call with tool_name to get detailed schema",
        }

    if request.tool_name not in tools:
        return {
            "error": f"Unknown tool: {request.tool_name}",
            "available_tools": list(tools.keys()),
        }

    return tools[request.tool_name].model_json_schema()
