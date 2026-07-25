"""
EDGAR SEC Meta-Tools for LLM Context Optimization.

Consolidates individual tools into domain-based meta-tools.
Each meta-tool supports action="help" for discovery.

Meta-tools:
- edgar_filings: Company submissions, facts, concepts, and frames
- edgar_analysis: Debt schedules, equity compensation, HTML table extraction
- edgar_documents: Fetch actual document text from SEC filings
- edgar_lookup: CIK lookup and health check
- edgar_schema: Tool introspection
"""

import sys
from pathlib import Path
from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from mcp_schema import OutputBaseModel
from pydantic import Field

# Add parent directory to path for relative imports (consistent with other tool files)
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import EDGAR_OFFLINE_MODE
from models import (
    ExtractHTMLTableInput,
    GetCompanyConceptInput,
    GetCompanyFactsInput,
    GetCompanySubmissionsInput,
    GetDebtScheduleInput,
    GetEquityCompensationInput,
    GetFilingDocumentInput,
    GetFramesInput,
    ListFilingDocumentsInput,
    LookupCIKInput,
)
from tools.filings import (
    get_company_concept,
    get_company_facts,
    get_company_submissions,
    get_frames,
)
from tools.health import health_check
from tools.lookup import lookup_cik

# =============================================================================
# Helper Functions
# =============================================================================


def _get_error_message(e: Exception) -> str:
    """Extract clean error message from exception.

    KeyError uses repr() which adds spurious quotes, so we extract args[0] directly.
    For other exceptions, use str().
    """
    if isinstance(e, KeyError) and e.args:
        return str(e.args[0])
    return str(e)


# =============================================================================
# Output Models
# =============================================================================


class HelpResponse(OutputBaseModel):
    """Response for action=help requests."""

    tool_name: str = Field(..., description="Meta-tool name to introspect.")
    description: str = Field(..., description="Detailed description. Optional but recommended.")
    actions: dict[str, dict[str, Any]] = Field(
        ..., description="Available actions with required_params, optional_params, description"
    )


class MetaToolOutput(OutputBaseModel):
    """Standard output wrapper for all edgar meta-tools.

    Since meta-tools dispatch to multiple sub-tools with different output types,
    this wrapper provides a consistent structure while preserving the actual result.
    """

    action: str = Field(
        ..., description="The operation to perform. REQUIRED. Call with action='help' first."
    )
    success: bool = Field(True, description="Whether the operation succeeded")
    result: dict = Field(..., description="The result data from the underlying operation")
    error: str | None = Field(None, description="Error message if success=False")


# =============================================================================
# Input Models for Meta-Tools
# =============================================================================


class FilingsInput(BaseModel):
    """Input for edgar_filings meta-tool."""

    action: Literal["help", "submissions", "facts", "concept", "frames"] = Field(
        description="Operation to perform"
    )
    # Company identifiers - at least ONE is REQUIRED for submissions/facts/concept
    cik: str | None = Field(
        None,
        description="10-digit zero-padded CIK. ONE OF cik/ticker/name REQUIRED for submissions, facts, concept.",
    )
    ticker: str | None = Field(
        None,
        description="Stock ticker (e.g., 'AAPL'). ONE OF cik/ticker/name REQUIRED for submissions, facts, concept.",
    )
    name: str | None = Field(
        None,
        description="Company name for fuzzy search. ONE OF cik/ticker/name REQUIRED for submissions, facts, concept.",
    )
    # Submissions - pagination and filtering
    limit: int | None = Field(20, description="Max results to return. Typical range: 1-100.")
    page: int | None = Field(
        1, description="Page number (1-indexed). Use with limit for pagination."
    )
    form_types: list[str] | None = Field(
        None,
        description=(
            "Filter by SEC form types (case-insensitive) (e.g., ['10-K', '10-Q']). "
            "Common: 10-K (annual), 10-Q (quarterly), 8-K (events), DEF 14A (proxy)"
        ),
    )
    summary_only: bool = Field(
        False,
        description=(
            "If True, return only summary statistics without full filing details. "
            "Use to discover what filings exist before fetching specific ones."
        ),
    )
    # Facts
    limit_concepts: int | None = Field(50, description="Max concepts per taxonomy (max 100)")
    # Concept
    taxonomy: str | None = Field(
        None,
        description=(
            "XBRL taxonomy namespace. Use 'us-gaap' for financial metrics, "
            "'dei' for document info. Case-sensitive lowercase."
        ),
    )
    tag: str | None = Field(
        None,
        description=(
            "XBRL concept tag name. Case-sensitive PascalCase. "
            "Common tags: 'Revenues', 'NetIncomeLoss', 'Assets', "
            "'CashAndCashEquivalentsAtCarryingValue', 'LongTermDebt'. "
            "TIP: Use action='facts' first to discover exact tag names."
        ),
    )
    # Frames
    unit: str | None = Field(
        None,
        description=(
            "Unit of measure for frames action. Common values: "
            "'USD' (monetary), 'shares', 'pure' (ratios), 'USD-per-shares' (EPS)."
        ),
    )
    period: str | None = Field(
        None,
        description=(
            "Reporting period for frames action. "
            "DURATION (income/cash flow items): 'CY2023' (annual), 'CY2023Q4' (quarterly). "
            "INSTANT (balance sheet items): add 'I' suffix, e.g., 'CY2023Q4I'. "
            "Use 'I' for Assets, Cash, Debt; no suffix for Revenue, NetIncome."
        ),
    )


class AnalysisInput(BaseModel):
    """Input for edgar_analysis meta-tool."""

    action: Literal["help", "debt_schedule", "equity_compensation", "html_table"] = Field(
        description="Operation to perform"
    )
    # Company identifiers - at least ONE is REQUIRED for ALL analysis actions
    cik: str | None = Field(
        None,
        description="10-digit zero-padded CIK. ONE OF cik/ticker/name REQUIRED for all analysis actions.",
    )
    ticker: str | None = Field(
        None,
        description="Stock ticker (e.g., 'AAPL'). ONE OF cik/ticker/name REQUIRED for all analysis actions.",
    )
    name: str | None = Field(
        None,
        description="Company name. ONE OF cik/ticker/name REQUIRED for all analysis actions.",
    )
    # Filing reference - REQUIRED for all analysis actions
    filing_accession: str | None = Field(
        None,
        description="Filing accession number (e.g., '0000320193-23-000106'). REQUIRED for all analysis actions. Obtain from edgar_filings action='submissions' results.",
    )
    # HTML table extraction
    table_keyword: str | None = Field(
        None,
        description="Keyword to search in table headers. REQUIRED for html_table action. Examples: 'debt', 'revenue', 'stock option'. Start with broader terms.",
    )


class LookupInput(BaseModel):
    """Input for edgar_lookup meta-tool."""

    action: Literal["help", "cik", "health"] = Field(description="Operation to perform")
    ticker: str | None = Field(
        None,
        description="Stock ticker (e.g., 'AAPL'). ONE OF ticker/name REQUIRED for cik action.",
    )
    name: str | None = Field(
        None,
        description="Company name for fuzzy search. ONE OF ticker/name REQUIRED for cik action.",
    )


class SchemaInput(BaseModel):
    """Input for edgar_schema meta-tool."""

    tool_name: str | None = Field(
        None, description="Tool name to get schema for. If None, lists all tools."
    )


class DocumentsInput(BaseModel):
    """Input for edgar_documents meta-tool."""

    action: Literal["help", "list", "get_text"] = Field(description="Operation to perform")
    cik: str | None = Field(
        None,
        description="10-digit CIK. ONE OF cik/ticker/name REQUIRED.",
    )
    ticker: str | None = Field(
        None,
        description="Stock ticker (e.g., 'AAPL'). ONE OF cik/ticker/name REQUIRED.",
    )
    name: str | None = Field(
        None,
        description="Company name. ONE OF cik/ticker/name REQUIRED.",
    )
    filing_accession: str | None = Field(
        None,
        description="Accession number (e.g., '0001193125-25-309873'). REQUIRED for list and get_text. Obtain from edgar_filings action='submissions' results.",
    )
    document: str = Field(
        "primary",
        description="Which document: 'primary' or filename from list action. Used by get_text. Use action='list' first to see available filenames. Common files: 'ex10-1.htm', 'ex21-1.htm'.",
    )


# =============================================================================
# Meta-Tool Functions
# =============================================================================


async def edgar_filings(request: FilingsInput) -> MetaToolOutput:
    """Access SEC EDGAR company filings, XBRL facts, and financial concepts.

    Start with action='help' to see available operations.
    Typical workflow:
    1. action='submissions' - list filings for a company
    2. action='facts' - get all financial metrics
    3. action='concept' - get specific metric history
    4. action='frames' - compare metric across companies
    """
    if request.action == "help":
        help_data = HelpResponse(
            tool_name="edgar_filings",
            description="Access SEC EDGAR company submissions, XBRL facts, concepts, and frames.",
            actions={
                "submissions": {
                    "description": (
                        "Get SEC filings for a company with smart pagination. "
                        "TIP: Use summary_only=True first to see what's available, "
                        "then filter by form_types and paginate."
                    ),
                    "required_params": [],
                    "required_one_of": ["cik", "ticker", "name"],
                    "optional_params": ["limit", "page", "form_types", "summary_only"],
                    "example": {
                        "summary": {"ticker": "AAPL", "summary_only": True},
                        "10k_filings": {"ticker": "AAPL", "form_types": ["10-K"], "limit": 10},
                        "paginate": {"ticker": "AAPL", "page": 2, "limit": 20},
                    },
                },
                "facts": {
                    "description": "Get all XBRL facts for a company",
                    "required_params": [],
                    "required_one_of": ["cik", "ticker", "name"],
                    "optional_params": ["limit_concepts"],
                },
                "concept": {
                    "description": "Get all historical values for a specific XBRL concept",
                    "required_params": ["taxonomy", "tag"],
                    "required_one_of": ["cik", "ticker", "name"],
                    "optional_params": [],
                    "example": {
                        "revenue": {"ticker": "AAPL", "taxonomy": "us-gaap", "tag": "Revenues"},
                        "net_income": {
                            "ticker": "AAPL",
                            "taxonomy": "us-gaap",
                            "tag": "NetIncomeLoss",
                        },
                    },
                },
                "frames": {
                    "description": "Get aggregated XBRL data across all companies for a period",
                    "required_params": ["taxonomy", "tag", "unit", "period"],
                    "optional_params": [],
                    "example": {
                        "revenue_2023": {
                            "taxonomy": "us-gaap",
                            "tag": "Revenues",
                            "unit": "USD",
                            "period": "CY2023",
                        },
                        "assets_q4_2023": {
                            "taxonomy": "us-gaap",
                            "tag": "Assets",
                            "unit": "USD",
                            "period": "CY2023Q4I",
                        },
                    },
                    "period_format": (
                        "DURATION (income/cash flow): 'CY2023' (annual), 'CY2023Q4' (quarterly). "
                        "INSTANT (balance sheet): add 'I' suffix, e.g., 'CY2023Q4I'."
                    ),
                },
            },
        )
        return MetaToolOutput(action="help", result=help_data.model_dump())

    if request.action == "submissions":
        if not request.cik and not request.ticker and not request.name:
            return MetaToolOutput(
                action="submissions",
                success=False,
                result={},
                error="Missing company identifier. Provide 'cik' (e.g., '0000320193'), 'ticker' (e.g., 'AAPL'), OR 'name' (e.g., 'Apple Inc').",
            )
        try:
            result = await get_company_submissions(
                GetCompanySubmissionsInput(
                    cik=request.cik,
                    ticker=request.ticker,
                    name=request.name,
                    limit=request.limit,
                    page=request.page,
                    form_types=request.form_types,
                    summary_only=request.summary_only,
                )
            )
            return MetaToolOutput(action="submissions", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="submissions",
                success=False,
                result={},
                error=error_msg,
            )
    elif request.action == "facts":
        if not request.cik and not request.ticker and not request.name:
            return MetaToolOutput(
                action="facts",
                success=False,
                result={},
                error="Missing company identifier. Provide 'cik' (e.g., '0000320193'), 'ticker' (e.g., 'AAPL'), OR 'name' (e.g., 'Apple Inc').",
            )
        try:
            result = await get_company_facts(
                GetCompanyFactsInput(
                    cik=request.cik,
                    ticker=request.ticker,
                    name=request.name,
                    limit_concepts=request.limit_concepts,
                )
            )
            return MetaToolOutput(action="facts", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="facts",
                success=False,
                result={},
                error=error_msg,
            )
    elif request.action == "concept":
        if not request.taxonomy or not request.tag:
            return MetaToolOutput(
                action="concept",
                success=False,
                result={},
                error="Missing required parameters. Provide 'taxonomy' (e.g., 'us-gaap') AND 'tag' (e.g., 'Revenues', 'NetIncomeLoss').",
            )
        if not request.cik and not request.ticker and not request.name:
            return MetaToolOutput(
                action="concept",
                success=False,
                result={},
                error="Missing company identifier. Provide 'cik' (e.g., '0000320193'), 'ticker' (e.g., 'AAPL'), OR 'name' (e.g., 'Apple Inc').",
            )
        try:
            result = await get_company_concept(
                GetCompanyConceptInput(
                    cik=request.cik,
                    ticker=request.ticker,
                    name=request.name,
                    taxonomy=request.taxonomy,
                    tag=request.tag,
                )
            )
            return MetaToolOutput(action="concept", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            # Handle both offline (KeyError/FileNotFoundError) and online (HTTPStatusError) errors
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="concept",
                success=False,
                result={},
                error=error_msg,
            )
    elif request.action == "frames":
        if not request.taxonomy or not request.tag or not request.unit or not request.period:
            return MetaToolOutput(
                action="frames",
                success=False,
                result={},
                error="Missing required parameters. Provide 'taxonomy' (e.g., 'us-gaap'), 'tag' (e.g., 'Assets'), 'unit' (e.g., 'USD'), AND 'period' (e.g., 'CY2023Q4I' for balance sheet items).",
            )
        try:
            result = await get_frames(
                GetFramesInput(
                    taxonomy=request.taxonomy,
                    tag=request.tag,
                    unit=request.unit,
                    period=request.period,
                )
            )
            return MetaToolOutput(action="frames", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="frames",
                success=False,
                result={},
                error=error_msg,
            )

    return MetaToolOutput(
        action=request.action, success=False, result={}, error=f"Unknown action: {request.action}"
    )


async def edgar_analysis(request: AnalysisInput) -> MetaToolOutput:
    """Extract structured financial data from SEC filings.

    Start with action='help' to see available operations.
    Requires a filing_accession from edgar_filings action='submissions'.
    Typical workflow:
    1. Get filing list: edgar_filings action='submissions' ticker='AAPL'
    2. Extract data: edgar_analysis action='debt_schedule' filing_accession='...'
    """
    if request.action == "help":
        actions = {
            "debt_schedule": {
                "description": "Extract debt schedule from filings",
                "required_params": ["filing_accession"],
                "required_one_of": ["cik", "ticker", "name"],
                "optional_params": [],
            },
            "equity_compensation": {
                "description": "Extract equity compensation data",
                "required_params": ["filing_accession"],
                "required_one_of": ["cik", "ticker", "name"],
                "optional_params": [],
            },
            "html_table": {
                "description": "Extract specific table from filing HTML",
                "required_params": ["filing_accession", "table_keyword"],
                "required_one_of": ["cik", "ticker", "name"],
                "optional_params": [],
            },
        }

        # Note which actions are online-only
        if EDGAR_OFFLINE_MODE:
            for action in actions:
                actions[action]["note"] = "Requires online mode"

        help_data = HelpResponse(
            tool_name="edgar_analysis",
            description="Extract debt schedules, equity compensation, and tables from SEC filings.",
            actions=actions,
        )
        return MetaToolOutput(action="help", result=help_data.model_dump())

    # These tools require online mode
    if EDGAR_OFFLINE_MODE:
        return MetaToolOutput(
            action=request.action,
            success=False,
            result={},
            error="Analysis tools require online mode (EDGAR_OFFLINE_MODE=False)",
        )

    # Import online-only tools
    from tools.debt import get_debt_schedule
    from tools.equity import get_equity_compensation
    from tools.html_tables import extract_html_table

    if request.action == "debt_schedule":
        if not request.filing_accession:
            return MetaToolOutput(
                action="debt_schedule",
                success=False,
                result={},
                error="Missing 'filing_accession'. First use edgar_filings action='submissions' to get accession numbers, then provide one here (e.g., '0000320193-24-000081').",
            )
        if not request.cik and not request.ticker and not request.name:
            return MetaToolOutput(
                action="debt_schedule",
                success=False,
                result={},
                error="Missing company identifier. Provide 'cik' (e.g., '0000320193'), 'ticker' (e.g., 'AAPL'), OR 'name' (e.g., 'Apple Inc').",
            )
        try:
            result = await get_debt_schedule(
                GetDebtScheduleInput(
                    cik=request.cik,
                    ticker=request.ticker,
                    name=request.name,
                    filing_accession=request.filing_accession,
                )
            )
            return MetaToolOutput(action="debt_schedule", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="debt_schedule",
                success=False,
                result={},
                error=error_msg,
            )
    elif request.action == "equity_compensation":
        if not request.filing_accession:
            return MetaToolOutput(
                action="equity_compensation",
                success=False,
                result={},
                error="Missing 'filing_accession'. First use edgar_filings action='submissions' to get accession numbers, then provide one here (e.g., '0000320193-24-000081').",
            )
        if not request.cik and not request.ticker and not request.name:
            return MetaToolOutput(
                action="equity_compensation",
                success=False,
                result={},
                error="Missing company identifier. Provide 'cik' (e.g., '0000320193'), 'ticker' (e.g., 'AAPL'), OR 'name' (e.g., 'Apple Inc').",
            )
        try:
            result = await get_equity_compensation(
                GetEquityCompensationInput(
                    cik=request.cik,
                    ticker=request.ticker,
                    name=request.name,
                    filing_accession=request.filing_accession,
                )
            )
            return MetaToolOutput(action="equity_compensation", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            # Handle errors from both offline and online modes
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="equity_compensation",
                success=False,
                result={},
                error=error_msg,
            )
    elif request.action == "html_table":
        if not request.filing_accession or not request.table_keyword:
            return MetaToolOutput(
                action="html_table",
                success=False,
                result={},
                error="Missing required parameters. Provide 'filing_accession' (from edgar_filings submissions) AND 'table_keyword' (e.g., 'debt', 'stock option').",
            )
        if not request.cik and not request.ticker and not request.name:
            return MetaToolOutput(
                action="html_table",
                success=False,
                result={},
                error="Missing company identifier. Provide 'cik' (e.g., '0000320193'), 'ticker' (e.g., 'AAPL'), OR 'name' (e.g., 'Apple Inc').",
            )
        try:
            result = await extract_html_table(
                ExtractHTMLTableInput(
                    cik=request.cik,
                    ticker=request.ticker,
                    name=request.name,
                    filing_accession=request.filing_accession,
                    table_keyword=request.table_keyword,
                )
            )
            return MetaToolOutput(action="html_table", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="html_table",
                success=False,
                result={},
                error=error_msg,
            )

    return MetaToolOutput(
        action=request.action, success=False, result={}, error=f"Unknown action: {request.action}"
    )


async def edgar_lookup(request: LookupInput) -> MetaToolOutput:
    """Look up company CIK from ticker/name and check server health.

    Start with action='help' to see available operations.
    Use action='cik' to find a company's SEC identifier.
    Use action='health' to check if server is in online or offline mode.
    """
    if request.action == "help":
        help_data = HelpResponse(
            tool_name="edgar_lookup",
            description="Look up company CIK from ticker/name and check server health.",
            actions={
                "cik": {
                    "description": "Map ticker or company name to 10-digit CIK",
                    "required_params": [],
                    "required_one_of": ["ticker", "name"],
                    "optional_params": [],
                },
                "health": {
                    "description": "Check server health and mode",
                    "required_params": [],
                    "optional_params": [],
                },
            },
        )
        return MetaToolOutput(action="help", result=help_data.model_dump())

    if request.action == "cik":
        if not request.ticker and not request.name:
            return MetaToolOutput(
                action="cik",
                success=False,
                result={},
                error="Missing identifier. Provide 'ticker' (e.g., 'AAPL') OR 'name' (e.g., 'Apple Inc') to look up CIK.",
            )
        try:
            result = await lookup_cik(LookupCIKInput(ticker=request.ticker, name=request.name))
            return MetaToolOutput(action="cik", result=result.model_dump())
        except (KeyError, FileNotFoundError, ValueError, Exception) as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="cik",
                success=False,
                result={},
                error=error_msg,
            )
    elif request.action == "health":
        try:
            result = await health_check()
            # health_check returns dict, wrap it directly
            return MetaToolOutput(
                action="health", result=result if isinstance(result, dict) else result.model_dump()
            )
        except Exception as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(
                action="health",
                success=False,
                result={},
                error=error_msg,
            )

    return MetaToolOutput(
        action=request.action, success=False, result={}, error=f"Unknown action: {request.action}"
    )


async def edgar_documents(request: DocumentsInput) -> MetaToolOutput:
    """Fetch actual document text from SEC filings (8-K exhibits, merger agreements, etc.).

    Start with action='help' to see available operations.
    Requires a filing_accession from edgar_filings action='submissions'.
    Typical workflow:
    1. Get filing list: edgar_filings action='submissions' ticker='AAPL'
    2. List documents: edgar_documents action='list' filing_accession='...'
    3. Get text: edgar_documents action='get_text' filing_accession='...' document='ex10-1.htm'
    """
    from tools.documents import get_filing_document, list_filing_documents

    if request.action == "help":
        actions = {
            "list": {
                "description": "List all documents in a filing (primary doc + exhibits)",
                "required_params": ["filing_accession"],
                "required_one_of": ["cik", "ticker", "name"],
                "optional_params": [],
                "example": {"ticker": "AAPL", "filing_accession": "0000320193-24-000123"},
            },
            "get_text": {
                "description": "Get the text content of a document",
                "required_params": ["filing_accession"],
                "required_one_of": ["cik", "ticker", "name"],
                "optional_params": ["document"],
                "example": {
                    "primary": {"ticker": "AAPL", "filing_accession": "0000320193-24-000123"},
                    "exhibit": {
                        "ticker": "AAPL",
                        "filing_accession": "0000320193-24-000123",
                        "document": "ex10-1.htm",
                    },
                },
            },
        }

        if EDGAR_OFFLINE_MODE:
            actions["list"]["note"] = (
                "Offline: returns primary document info from submissions JSON only. "
                "Full exhibit list requires online mode."
            )
            actions["get_text"]["note"] = (
                "Offline: requires filings/ directory with downloaded HTML files. "
                "Only primary document name is available from submissions."
            )

        help_data = HelpResponse(
            tool_name="edgar_documents",
            description="Fetch actual document text from SEC filings (8-K exhibits, merger agreements, etc.)",
            actions=actions,
        )
        return MetaToolOutput(action="help", result=help_data.model_dump())

    # Validate required params
    if not request.filing_accession:
        return MetaToolOutput(
            action=request.action,
            success=False,
            result={},
            error="Missing 'filing_accession'. First use edgar_filings action='submissions' to get accession numbers (e.g., '0000320193-24-000081').",
        )

    if not request.cik and not request.ticker and not request.name:
        return MetaToolOutput(
            action=request.action,
            success=False,
            result={},
            error="Missing company identifier. Provide 'cik' (e.g., '0000320193'), 'ticker' (e.g., 'AAPL'), OR 'name' (e.g., 'Apple Inc').",
        )

    if request.action == "list":
        try:
            input_model = ListFilingDocumentsInput(
                cik=request.cik,
                ticker=request.ticker,
                name=request.name,
                filing_accession=request.filing_accession,
            )
            result = await list_filing_documents(input_model)
            return MetaToolOutput(action="list", result=result.model_dump())
        except Exception as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(action="list", success=False, result={}, error=error_msg)

    elif request.action == "get_text":
        try:
            input_model = GetFilingDocumentInput(
                cik=request.cik,
                ticker=request.ticker,
                name=request.name,
                filing_accession=request.filing_accession,
                document=request.document,
            )
            result = await get_filing_document(input_model)
            return MetaToolOutput(action="get_text", result=result.model_dump())
        except Exception as e:
            error_msg = _get_error_message(e)
            return MetaToolOutput(action="get_text", success=False, result={}, error=error_msg)

    return MetaToolOutput(
        action=request.action, success=False, result={}, error=f"Unknown action: {request.action}"
    )


async def edgar_schema(request: SchemaInput) -> MetaToolOutput:
    """Get JSON schema for EDGAR SEC tools for introspection.

    Call with no tool_name to list all available tools.
    Call with a specific tool_name to get its detailed input schema.
    """
    tools = {
        "edgar_filings": FilingsInput,
        "edgar_analysis": AnalysisInput,
        "edgar_documents": DocumentsInput,
        "edgar_lookup": LookupInput,
        "edgar_schema": SchemaInput,
    }

    if request.tool_name is None:
        return MetaToolOutput(
            action="list",
            result={
                "available_tools": list(tools.keys()),
                "usage": "Call with tool_name to get detailed schema",
            },
        )

    if request.tool_name not in tools:
        return MetaToolOutput(
            action="schema",
            success=False,
            result={},
            error=f"Unknown tool: {request.tool_name}. Available: {list(tools.keys())}",
        )

    return MetaToolOutput(action="schema", result=tools[request.tool_name].model_json_schema())
