"""Equity compensation extraction tools.

Implements multi-tier retrieval strategy:
- Tier 1: Try XBRL API (fastest)
- Tier 2: Parse XBRL files with edgartools (if API incomplete)
- Tier 3: Scrape HTML tables (last resort - not yet implemented)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from models import (
    ESPPActivity,
    GetEquityCompensationInput,
    GetEquityCompensationOutput,
    PSUActivity,
    RSUActivity,
    StockOptionActivity,
)
from utils.api_client import rate_limited_get
from utils.cik_resolver import resolve_cik
from utils.xbrl_parser import extract_equity_compensation_xbrl


async def get_equity_compensation(
    request: GetEquityCompensationInput,
) -> GetEquityCompensationOutput:
    """Extract detailed equity compensation data from SEC filings."""
    # Implements multi-tier retrieval strategy:
    # 1. Tier 1 (XBRL API): Try Company Facts API for standard tags
    # 2. Tier 2 (XBRL Parser): Parse XBRL files with edgartools for dimensional data
    # 3. Tier 3 (HTML Scraper): Parse HTML tables as last resort (not yet implemented)
    cik_padded = await resolve_cik(request.cik, request.ticker, request.name)

    # Tier 1: Try XBRL API
    logger.info(f"Attempting Tier 1 (XBRL API) for {request.filing_accession}")
    tier1_result = await _try_xbrl_api(cik_padded, request.filing_accession)
    if tier1_result:
        return tier1_result

    # Tier 2: Try XBRL Parser
    logger.info(f"Tier 1 failed, attempting Tier 2 (XBRL Parser) for {request.filing_accession}")
    tier2_result = await _try_xbrl_parser(cik_padded, request.filing_accession)
    if tier2_result:
        return tier2_result

    # Tier 3: Try HTML Scraper (not yet implemented)
    logger.warning("Tier 2 failed, Tier 3 (HTML Scraper) not yet implemented")

    # All tiers failed
    raise ValueError(
        f"Unable to extract equity compensation data for {request.filing_accession}. "
        "Manual review required."
    )


async def _try_xbrl_api(cik: str, filing_accession: str) -> GetEquityCompensationOutput | None:
    """Attempt to extract equity compensation from XBRL Company Facts API."""
    try:
        response = await rate_limited_get(f"/api/xbrl/companyfacts/CIK{cik}.json")
        response.raise_for_status()
        data = response.json()

        # Check if we have the required equity compensation tags
        us_gaap_facts = data.get("facts", {}).get("us-gaap", {})

        # Look for common stock-based compensation tags
        option_tags = [
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsGrantsInPeriod",
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsExercisesInPeriod",
        ]

        rsu_tags = [
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvestedNumber",
        ]

        # Check if API has sufficient data
        # Only check for RSUs since that's the only extraction currently implemented
        has_options = any(tag in us_gaap_facts for tag in option_tags)  # noqa: F841
        has_rsus = any(tag in us_gaap_facts for tag in rsu_tags)

        if not has_rsus:
            logger.debug("XBRL API missing RSU tags")
            return None

        # Parse facts and construct output
        result = {}

        # Helper function to find matching fact by accession
        def find_fact_by_accession(tag_data, target_accn):
            """Find fact matching the filing accession number."""
            if not tag_data or "units" not in tag_data:
                return None

            # Check all unit types (USD, shares, etc.)
            for unit_type, facts in tag_data["units"].items():
                for fact in facts:
                    # Match by accession number
                    if fact.get("accn") == target_accn:
                        return fact
            return None

        # Extract RSU data if available
        base_rsu_tag = "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvested"  # noqa: E501
        rsu_tags_map = {
            "unvested_beginning": f"{base_rsu_tag}Number",
            "granted": f"{base_rsu_tag}GrantsInPeriod",
            "vested": f"{base_rsu_tag}VestedInPeriod",
            "forfeited": f"{base_rsu_tag}ForfeitedInPeriod",
        }

        rsu_data = {}
        for field, tag in rsu_tags_map.items():
            if tag in us_gaap_facts:
                fact = find_fact_by_accession(us_gaap_facts[tag], filing_accession)
                if fact:
                    val = fact.get("val")
                    rsu_data[field] = int(val) if val is not None else None

        # Calculate unvested_ending for RSUs
        if rsu_data and "unvested_beginning" in rsu_data:
            unvested_ending = rsu_data["unvested_beginning"] or 0
            unvested_ending += rsu_data.get("granted", 0) or 0
            unvested_ending -= abs(rsu_data.get("vested", 0) or 0)
            unvested_ending -= abs(rsu_data.get("forfeited", 0) or 0)
            rsu_data["unvested_ending"] = unvested_ending
            result["rsus"] = rsu_data

        # If we found data, construct and return output
        if result:
            output = GetEquityCompensationOutput(
                cik=cik,
                filing_accession=filing_accession,
                report_date=None,  # API doesn't provide report date easily
                rsus=RSUActivity(**result["rsus"]) if "rsus" in result else None,
                stock_options=None,  # Could implement similar to RSUs
                psus=None,  # Could implement similar to RSUs
                espp=None,  # Could implement similar to RSUs
                data_source="xbrl_api",
                confidence="high",
            )
            logger.info(f"Successfully extracted equity data from XBRL API for {filing_accession}")
            return output

        # API has tags but no matching data for this filing
        logger.debug(f"XBRL API has tags but no facts matching accession {filing_accession}")
        return None

    except Exception as e:
        logger.warning(f"XBRL API extraction failed: {e}")
        return None


async def _try_xbrl_parser(cik: str, filing_accession: str) -> GetEquityCompensationOutput | None:
    """Attempt to extract equity compensation from XBRL files using edgartools."""
    try:
        equity_data = extract_equity_compensation_xbrl(filing_accession)

        if not equity_data:
            logger.debug("XBRL parser returned no data")
            return None

        # Construct output model
        output = GetEquityCompensationOutput(
            cik=cik,
            filing_accession=filing_accession,
            report_date=equity_data.get("report_date"),
            stock_options=(
                StockOptionActivity(**equity_data["stock_options"])
                if "stock_options" in equity_data
                else None
            ),
            rsus=RSUActivity(**equity_data["rsus"]) if "rsus" in equity_data else None,
            psus=PSUActivity(**equity_data["psus"]) if "psus" in equity_data else None,
            espp=ESPPActivity(**equity_data["espp"]) if "espp" in equity_data else None,
            data_source="xbrl_parser",
            confidence="high",
        )

        logger.info(f"Successfully extracted equity data from XBRL parser for {filing_accession}")
        return output

    except Exception as e:
        logger.warning(f"XBRL parser extraction failed: {e}")
        return None
