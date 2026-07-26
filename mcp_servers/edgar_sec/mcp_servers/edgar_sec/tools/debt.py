"""Debt schedule extraction tools.

Implements multi-tier retrieval strategy:
- Tier 1: Try XBRL API (fastest)
- Tier 2: Parse XBRL files with edgartools (if API incomplete)
- Tier 3: Scrape HTML tables (last resort)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from models import (
    DebtInstrument,
    GetDebtScheduleInput,
    GetDebtScheduleOutput,
)
from utils.api_client import rate_limited_get
from utils.cik_resolver import resolve_cik
from utils.html_parser import extract_debt_schedule_table
from utils.xbrl_parser import extract_debt_schedule_xbrl, get_filing_from_accession


async def get_debt_schedule(request: GetDebtScheduleInput) -> GetDebtScheduleOutput:
    """Extract debt schedule from SEC filings."""

    # Implements multi-tier retrieval strategy:
    # 1. Tier 1 (XBRL API): Try Company Facts API for DebtCurrent and DebtNoncurrent
    # 2. Tier 2 (XBRL Parser): Parse XBRL files with edgartools for dimensional data
    # 3. Tier 3 (HTML Scraper): Parse HTML tables as last resort
    cik_padded = await resolve_cik(request.cik, request.ticker, request.name)

    logger.info(f"Attempting Tier 1 (XBRL API) for {request.filing_accession}")
    tier1_result = await _try_xbrl_api(cik_padded, request.filing_accession)
    if tier1_result:
        return tier1_result

    logger.info(f"Tier 1 failed, attempting Tier 2 (XBRL Parser) for {request.filing_accession}")
    tier2_result = _try_xbrl_parser(cik_padded, request.filing_accession)
    if tier2_result:
        return tier2_result

    logger.info(f"Tier 2 failed, attempting Tier 3 (HTML Scraper) for {request.filing_accession}")
    tier3_result = _try_html_scraper(cik_padded, request.filing_accession)
    if tier3_result:
        return tier3_result

    raise ValueError(
        f"Unable to extract debt schedule data for {request.filing_accession}. "
        "Manual review required."
    )


def _find_fact_by_accession(tag_data: dict, target_accn: str) -> dict | None:
    """Find fact matching the filing accession number.

    Args:
        tag_data: XBRL tag data containing units and facts
        target_accn: Target accession number to match

    Returns:
        Matching fact dict or None
    """
    if not tag_data or "units" not in tag_data:
        return None

    for _unit_type, facts in tag_data["units"].items():
        for fact in facts:
            if fact.get("accn") == target_accn:
                return fact
    return None


async def _try_xbrl_api(cik: str, filing_accession: str) -> GetDebtScheduleOutput | None:
    """Attempt to extract debt schedule from XBRL Company Facts API."""
    try:
        response = await rate_limited_get(f"/api/xbrl/companyfacts/CIK{cik}.json")
        response.raise_for_status()
        data = response.json()

        us_gaap_facts = data.get("facts", {}).get("us-gaap", {})

        # Primary tags
        primary_current_tags = ["DebtCurrent"]
        primary_noncurrent_tags = ["DebtNoncurrent"]

        # Fallback current tags
        fallback_current_tags = [
            "LongTermDebtCurrent",
            "ShortTermBorrowings",
            "LinesOfCreditCurrent",
            "ConvertibleDebtCurrent",
        ]

        # Fallback noncurrent tags
        fallback_noncurrent_tags = [
            "LongTermDebtNoncurrent",
            "LongTermDebt",
            "LongTermDebtAndCapitalLeaseObligations",
            "ConvertibleDebtNoncurrent",
            "SecuredDebt",
            "UnsecuredDebt",
        ]

        # Total-only fallback (when no current/noncurrent split found)
        total_only_tags = [
            "DebtInstrumentCarryingAmount",
            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        ]

        def _try_tag_groups(tag_list, accession):
            """Try each tag in order, return value on first match."""
            for tag in tag_list:
                if tag in us_gaap_facts:
                    fact = _find_fact_by_accession(us_gaap_facts[tag], accession)
                    if fact:
                        return float(fact.get("val", 0))
            return None

        # Try primary tags first, then fall back per-component independently.
        # This ensures that if e.g. DebtCurrent exists but DebtNoncurrent doesn't,
        # we still try fallback noncurrent tags like LongTermDebt.
        debt_current_val = _try_tag_groups(primary_current_tags, filing_accession)
        debt_noncurrent_val = _try_tag_groups(primary_noncurrent_tags, filing_accession)
        used_fallback = False

        # Try fallback for whichever component is still missing
        if debt_current_val is None:
            fallback_current = _try_tag_groups(fallback_current_tags, filing_accession)
            if fallback_current is not None:
                debt_current_val = fallback_current
                used_fallback = True

        if debt_noncurrent_val is None:
            fallback_noncurrent = _try_tag_groups(fallback_noncurrent_tags, filing_accession)
            if fallback_noncurrent is not None:
                debt_noncurrent_val = fallback_noncurrent
                used_fallback = True

        # If still nothing, try total-only tags
        if debt_current_val is None and debt_noncurrent_val is None:
            total_val = _try_tag_groups(total_only_tags, filing_accession)
            if total_val is not None:
                # Assign entire amount as noncurrent (conservative default)
                debt_noncurrent_val = total_val
                used_fallback = True
                logger.debug("Using total-only tag as noncurrent fallback")

        if debt_current_val is None and debt_noncurrent_val is None:
            logger.debug("XBRL API missing debt tags (primary and fallback)")
            return None

        # At this point, at least one of the values is not None
        total_debt = (debt_current_val or 0) + (debt_noncurrent_val or 0)

        instruments = []
        if total_debt > 0:
            instruments.append(
                DebtInstrument(
                    instrument_name="Aggregate Debt",
                    current_portion=debt_current_val or 0.0,
                    noncurrent_portion=debt_noncurrent_val or 0.0,
                    maturity_date=None,
                )
            )

        # Use medium confidence for fallback tags, high for primary
        if used_fallback:
            confidence = "medium"
        else:
            confidence = "high" if len(instruments) > 0 else "medium"

        output = GetDebtScheduleOutput(
            cik=cik,
            filing_accession=filing_accession,
            report_date=None,
            debt_instruments=instruments,
            total_current_debt=debt_current_val or 0.0,
            total_noncurrent_debt=debt_noncurrent_val or 0.0,
            data_source="xbrl_api",
            confidence=confidence,
        )
        logger.info(f"Successfully extracted debt data from XBRL API for {filing_accession}")
        return output

    except Exception as e:
        logger.warning(f"XBRL API extraction failed: {e}")
        return None


def _try_xbrl_parser(cik: str, filing_accession: str) -> GetDebtScheduleOutput | None:
    """Attempt to extract debt schedule from XBRL files using edgartools."""
    try:
        debt_data = extract_debt_schedule_xbrl(filing_accession)

        if not debt_data:
            logger.debug("XBRL parser returned no data")
            return None

        instruments = [DebtInstrument(**inst) for inst in debt_data.get("debt_instruments", [])]

        output = GetDebtScheduleOutput(
            cik=cik,
            filing_accession=filing_accession,
            report_date=debt_data.get("report_date"),
            debt_instruments=instruments,
            total_current_debt=debt_data.get("total_current_debt", 0.0),
            total_noncurrent_debt=debt_data.get("total_noncurrent_debt", 0.0),
            data_source="xbrl_parser",
            confidence="high",
        )

        return output

    except Exception as e:
        logger.warning(f"XBRL parser extraction failed: {e}")
        return None


def _try_html_scraper(cik: str, filing_accession: str) -> GetDebtScheduleOutput | None:
    """Attempt to extract debt schedule from HTML tables.

    Args:
        cik: 10-digit zero-padded CIK
        filing_accession: Accession number

    Returns:
        GetDebtScheduleOutput with extracted data or None if scraping fails
    """
    try:
        logger.info(f"Fetching HTML document for {filing_accession}")
        filing = get_filing_from_accession(filing_accession)

        html_content = filing.html()

        if not html_content:
            logger.debug("No HTML content available")
            return None

        debt_data = extract_debt_schedule_table(html_content)

        if not debt_data:
            logger.debug("HTML scraper found no debt schedule tables")
            return None

        instruments = [DebtInstrument(**inst) for inst in debt_data.get("debt_instruments", [])]

        output = GetDebtScheduleOutput(
            cik=cik,
            filing_accession=filing_accession,
            report_date=None,
            debt_instruments=instruments,
            total_current_debt=debt_data.get("total_current_debt", 0.0),
            total_noncurrent_debt=debt_data.get("total_noncurrent_debt", 0.0),
            data_source="html_scraper",
            confidence=debt_data.get("confidence", "medium"),
        )

        logger.info(f"Successfully extracted debt data from HTML for {filing_accession}")
        return output

    except Exception as e:
        logger.warning(f"HTML scraper extraction failed: {e}")
        return None
