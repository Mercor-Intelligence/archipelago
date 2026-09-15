"""FMP MCP Server Models.

This module re-exports models from the models.fmp submodule for UI generation.
"""

import sys
from pathlib import Path

# Ensure we can import from the models subdirectory
_current_dir = Path(__file__).parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from models.fmp import (  # noqa: E402
    # Response Models
    CompanyProfile,
    CompanyProfileResponse,
    FinancialStatement,
    FinancialStatementResponse,
    StockQuote,
    # Request Models
    StockQuoteRequest,
    StockQuoteResponse,
    StockSearchResponse,
    StockSearchResult,
)

__all__ = [
    # Request Models
    "StockQuoteRequest",
    # Response Models
    "CompanyProfile",
    "CompanyProfileResponse",
    "FinancialStatement",
    "FinancialStatementResponse",
    "StockQuote",
    "StockQuoteResponse",
    "StockSearchResult",
    "StockSearchResponse",
]
