"""HTML table extraction tool for SEC filings.

Provides MCP tool for searching and extracting tables from HTML filing documents.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from models import ExtractHTMLTableInput, ExtractHTMLTableOutput
from utils.cik_resolver import resolve_cik
from utils.html_parser import extract_table_by_keyword
from utils.xbrl_parser import get_filing_from_accession


async def extract_html_table(request: ExtractHTMLTableInput) -> ExtractHTMLTableOutput:
    """Search for and extract a specific table from filing HTML."""
    try:
        await resolve_cik(request.cik, request.ticker, request.name)
    except Exception as e:
        logger.warning(f"CIK resolution failed: {e}")
        return ExtractHTMLTableOutput(
            tables=[],
            confidence="low",
            error=f"Failed to resolve company identifier: {e}",
        )

    if not request.table_keyword:
        return ExtractHTMLTableOutput(
            tables=[],
            confidence="low",
            error="table_keyword is required but was empty",
        )

    try:
        logger.info(f"Fetching HTML document for {request.filing_accession}")
        filing = get_filing_from_accession(request.filing_accession)
        html_content = filing.html()

        if not html_content:
            logger.debug("No HTML content available")
            return ExtractHTMLTableOutput(
                tables=[],
                confidence="low",
                error=(f"No HTML content available for filing {request.filing_accession}"),
            )

        if isinstance(html_content, bytes):
            html_content = html_content.decode("utf-8", errors="replace")

        table_result = extract_table_by_keyword(
            html_content=html_content,
            keyword=request.table_keyword,
            context_lines=10,
        )

        if not table_result or not table_result.get("found"):
            return ExtractHTMLTableOutput(
                tables=[],
                confidence="low",
                error=(f"No table found matching keyword '{request.table_keyword}'"),
            )

        confidence = table_result.get("confidence", "medium")

        # Reject low-confidence matches — they are almost always the
        # wrong table and produce noisy, misleading data.
        if confidence == "low":
            tables_scanned = table_result.get("tables_scanned", "?")
            match_reason = table_result.get("match_reason", "unknown")
            return ExtractHTMLTableOutput(
                tables=[],
                confidence="low",
                error=(
                    f"No confident table match for "
                    f"'{request.table_keyword}' "
                    f"(best match was '{match_reason}' across "
                    f"{tables_scanned} tables). Try a more specific "
                    f"keyword or use edgar_documents to read the "
                    f"filing directly."
                ),
            )

        tables = []
        if table_result.get("table_data"):
            tables.append(table_result["table_data"])

        return ExtractHTMLTableOutput(
            tables=tables,
            confidence=confidence,
            error=None,
        )

    except ValueError as e:
        logger.warning(f"Failed to fetch filing: {e}")
        return ExtractHTMLTableOutput(
            tables=[],
            confidence="low",
            error=f"Failed to fetch filing: {e}",
        )
    except Exception as e:
        logger.warning(f"HTML table extraction failed: {e}")
        return ExtractHTMLTableOutput(
            tables=[],
            confidence="low",
            error=f"Failed to extract table: {e}",
        )
