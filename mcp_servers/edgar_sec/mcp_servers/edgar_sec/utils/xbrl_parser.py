"""XBRL parsing utilities using edgartools.

This module provides a wrapper around edgartools for extracting dimensional data
from XBRL filings (equity compensation tables, debt schedules, etc.).
"""

import os
import re

from config import EDGAR_USER_AGENT
from loguru import logger


def get_filing_from_accession(accession: str):
    """Get a Filing object from edgartools using accession number.

    Args:
        accession: Accession number (e.g., "0001477720-25-000123")

    Returns:
        Filing object from edgartools

    Raises:
        ValueError: If filing cannot be fetched or parsed
    """
    try:
        # Lazy import to avoid import errors during testing
        from edgar import get_by_accession_number

        # Set user agent for edgar
        os.environ.setdefault("EDGAR_IDENTITY", EDGAR_USER_AGENT)

        # Load filing by accession number
        filing = get_by_accession_number(accession)
        if filing is None:
            raise ValueError(f"Filing not found for accession {accession}")
        logger.debug(f"Loaded filing {accession}")
        return filing
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to load filing {accession}: {e}")
        raise ValueError(f"Unable to load filing {accession}: {e}") from e


def extract_equity_compensation_xbrl(accession: str) -> dict | None:
    """Extract equity compensation data from XBRL filing using edgartools.

    This function attempts to extract stock option, RSU, PSU, and ESPP activity
    from XBRL dimensional tables.

    Args:
        accession: Accession number (e.g., "0001477720-25-000123")

    Returns:
        Dictionary with equity compensation data or None if extraction fails

    Example return structure:
        {
            "stock_options": {
                "outstanding_beginning": 1000000,
                "granted": 50000,
                "exercised": -20000,
                "forfeited": -5000,
                "expired": 0,
                "outstanding_ending": 1025000,
                "exercisable_ending": 500000,
                "weighted_avg_exercise_price_beginning": 25.50,
                "weighted_avg_exercise_price_granted": 30.00,
                "weighted_avg_exercise_price_exercised": 20.00,
                "weighted_avg_exercise_price_ending": 26.00,
            },
            "rsus": {
                "unvested_beginning": 18500,
                "granted": 5200,
                "vested": -4100,
                "forfeited": -305,
                "unvested_ending": 19295,
                "weighted_avg_grant_date_fair_value_beginning": 25.00,
                "weighted_avg_grant_date_fair_value_ending": 27.50,
            },
            "psus": {
                "unvested_beginning": 800,
                "granted": 200,
                "vested": -50,
                "forfeited": -6,
                "unvested_ending": 944,
                "weighted_avg_grant_date_fair_value": 28.00,
            },
            "espp": {
                "shares_available": 450,
                "shares_purchased": 100,
                "weighted_avg_purchase_price": 22.50,
            }
        }
    """
    try:
        filing = get_filing_from_accession(accession)

        # Try to get XBRL data from filing
        # edgartools filing.xbrl() returns XBRLInstance or None
        xbrl_instance = filing.xbrl()
        if xbrl_instance is None:
            logger.warning(f"Filing {accession} has no XBRL data")
            return None

        result = {}

        # Extract stock options data
        stock_options = _extract_stock_options(xbrl_instance)
        if stock_options:
            result["stock_options"] = stock_options

        # Extract RSU data
        rsus = _extract_rsus(xbrl_instance)
        if rsus:
            result["rsus"] = rsus

        # Extract PSU data
        psus = _extract_psus(xbrl_instance)
        if psus:
            result["psus"] = psus

        # Extract ESPP data
        espp = _extract_espp(xbrl_instance)
        if espp:
            result["espp"] = espp

        if not result:
            logger.warning(f"No equity compensation data found in XBRL for {accession}")
            return None

        logger.info(f"Successfully extracted equity compensation from XBRL: {accession}")
        return result

    except Exception as e:
        logger.error(f"Failed to extract equity compensation from XBRL: {e}")
        return None


def _extract_stock_options(xbrl_instance) -> dict | None:
    """Extract stock option activity from XBRL instance.

    Args:
        xbrl_instance: XBRLInstance object from edgar tools

    Returns:
        Dictionary with stock option data or None
    """
    try:
        # Map of XBRL concepts to our output fields
        concept_map = {
            "outstanding_beginning": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",  # noqa: E501
            "granted": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsGrantsInPeriodGross",  # noqa: E501
            "exercised": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsExercisesInPeriod",  # noqa: E501
            "forfeited": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsForfeituresInPeriod",  # noqa: E501
            "expired": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsExpirationsInPeriod",  # noqa: E501
        }

        result = {}

        # Try to find stock option facts
        for field, concept in concept_map.items():
            try:
                # Query for facts matching this concept using edgartools API
                matching_facts = xbrl_instance.facts.query().by_concept(concept).to_dataframe()
                if not matching_facts.empty:
                    # Get the most recent value
                    latest_fact = matching_facts.iloc[-1]
                    value = latest_fact.get("value")
                    result[field] = int(value) if value is not None else None
            except Exception as e:
                logger.debug(f"Could not find {field} for stock options: {e}")
                continue

        # Calculate outstanding_ending if we have the components
        if "outstanding_beginning" in result:
            outstanding_ending = result["outstanding_beginning"] or 0
            outstanding_ending += result.get("granted", 0) or 0
            outstanding_ending -= abs(result.get("exercised", 0) or 0)
            outstanding_ending -= abs(result.get("forfeited", 0) or 0)
            outstanding_ending -= abs(result.get("expired", 0) or 0)
            result["outstanding_ending"] = outstanding_ending

        # Only return if we found at least some data
        return result if result else None

    except Exception as e:
        logger.debug(f"Could not extract stock options: {e}")
        return None


def _extract_rsus(xbrl_instance) -> dict | None:
    """Extract RSU activity from XBRL instance.

    Args:
        xbrl_instance: XBRLInstance object from edgartools

    Returns:
        Dictionary with RSU data or None
    """
    try:
        # Map of XBRL concepts for RSUs
        # Common tag: ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvestedNumber  # noqa: E501
        base_concept = "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvested"  # noqa: E501

        concept_map = {
            "unvested_beginning": f"{base_concept}Number",
            "granted": f"{base_concept}GrantsInPeriod",
            "vested": f"{base_concept}VestedInPeriod",
            "forfeited": f"{base_concept}ForfeitedInPeriod",
        }

        result = {}

        # Try to find RSU facts with dimensional filtering
        for field, concept in concept_map.items():
            try:
                # Query for facts matching this concept
                df = xbrl_instance.facts.query().by_concept(concept).to_dataframe()

                if not df.empty:
                    # Filter by AwardTypeAxis dimension for RSUs
                    # Look for RestrictedStockUnitsRSUMember dimension
                    if "dim_us-gaap_AwardTypeAxis" in df.columns:
                        rsu_facts = df[
                            df["dim_us-gaap_AwardTypeAxis"].str.contains(
                                "RestrictedStockUnitsRSUMember", case=False, na=False
                            )
                        ]
                        if not rsu_facts.empty:
                            # Get the most recent value
                            latest_fact = rsu_facts.iloc[-1]
                            value = latest_fact.get("value")
                            result[field] = int(value) if value is not None else None
                    else:
                        # No dimensional data, use the last fact
                        latest_fact = df.iloc[-1]
                        value = latest_fact.get("value")
                        result[field] = int(value) if value is not None else None

            except Exception as e:
                logger.debug(f"Could not find {field} for RSUs: {e}")
                continue

        # Calculate unvested_ending if we have the components
        if "unvested_beginning" in result:
            unvested_ending = result["unvested_beginning"] or 0
            unvested_ending += result.get("granted", 0) or 0
            unvested_ending -= abs(result.get("vested", 0) or 0)
            unvested_ending -= abs(result.get("forfeited", 0) or 0)
            result["unvested_ending"] = unvested_ending

        return result if result else None

    except Exception as e:
        logger.debug(f"Could not extract RSUs: {e}")
        return None


def _extract_psus(xbrl_instance) -> dict | None:
    """Extract PSU activity from XBRL instance.

    Args:
        xbrl_instance: XBRLInstance object from edgartools

    Returns:
        Dictionary with PSU data or None
    """
    try:
        # PSUs use same base concept as RSUs but with PerformanceSharesMember dimension
        base_concept = "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvested"  # noqa: E501

        concept_map = {
            "unvested_beginning": f"{base_concept}Number",
            "granted": f"{base_concept}GrantsInPeriod",
            "vested": f"{base_concept}VestedInPeriod",
            "forfeited": f"{base_concept}ForfeitedInPeriod",
        }

        result = {}

        # Try to find PSU facts with dimensional filtering
        for field, concept in concept_map.items():
            try:
                # Query for facts matching this concept
                df = xbrl_instance.facts.query().by_concept(concept).to_dataframe()

                if not df.empty:
                    # Filter by AwardTypeAxis dimension for PSUs
                    # Look for Performance-related members (company-specific naming)
                    if "dim_us-gaap_AwardTypeAxis" in df.columns:
                        # PSU members commonly contain "Performance" in the name
                        psu_facts = df[
                            df["dim_us-gaap_AwardTypeAxis"].str.contains(
                                "Performance", case=False, na=False
                            )
                            & ~df["dim_us-gaap_AwardTypeAxis"].str.contains(
                                "RestrictedStockUnitsRSUMember", case=False, na=False
                            )
                        ]
                        if not psu_facts.empty:
                            # Get the most recent value
                            latest_fact = psu_facts.iloc[-1]
                            value = latest_fact.get("value")
                            result[field] = int(value) if value is not None else None

            except Exception as e:
                logger.debug(f"Could not find {field} for PSUs: {e}")
                continue

        # Calculate unvested_ending
        if "unvested_beginning" in result:
            unvested_ending = result["unvested_beginning"] or 0
            unvested_ending += result.get("granted", 0) or 0
            unvested_ending -= abs(result.get("vested", 0) or 0)
            unvested_ending -= abs(result.get("forfeited", 0) or 0)
            result["unvested_ending"] = unvested_ending

        return result if result else None

    except Exception as e:
        logger.debug(f"Could not extract PSUs: {e}")
        return None


def _extract_espp(xbrl_instance) -> dict | None:
    """Extract ESPP activity from XBRL instance.

    Args:
        xbrl_instance: XBRLInstance object from edgartools

    Returns:
        Dictionary with ESPP data or None
    """
    try:
        # ESPP concepts
        concept_map = {
            "shares_available": "ShareBasedCompensationArrangementByShareBasedPaymentAwardNumberOfSharesAvailableForGrant",  # noqa: E501
            "shares_purchased": "ShareBasedCompensationArrangementByShareBasedPaymentAwardSharesPurchasedForIssuance",  # noqa: E501
        }

        result = {}

        for field, concept in concept_map.items():
            try:
                # Query for facts matching this concept using edgartools API
                matching_facts = xbrl_instance.facts.query().by_concept(concept).to_dataframe()

                if not matching_facts.empty:
                    # Get the most recent value
                    latest_fact = matching_facts.iloc[-1]
                    value = latest_fact.get("value")
                    result[field] = int(value) if value is not None else None

            except Exception as e:
                logger.debug(f"Could not find {field} for ESPP: {e}")
                continue

        return result if result else None

    except Exception as e:
        logger.debug(f"Could not extract ESPP: {e}")
        return None


def extract_debt_schedule_xbrl(accession: str) -> dict | None:
    """Extract debt schedule from XBRL filing using edgartools.

    This function attempts to extract debt instruments with current/noncurrent
    breakdown from XBRL dimensional tables.

    Args:
        accession: Accession number (e.g., "0001477720-25-000123")

    Returns:
        Dictionary with debt schedule data or None if extraction fails.
        Format: {
            "report_date": "YYYY-MM-DD" or None,
            "debt_instruments": [{"instrument_name": ..., "current_portion": ...,
                                  "noncurrent_portion": ..., "maturity_date": ...}],
            "total_current_debt": float,
            "total_noncurrent_debt": float,
        }
    """
    try:
        filing = get_filing_from_accession(accession)

        xbrl_instance = filing.xbrl()
        if xbrl_instance is None:
            logger.warning(f"Filing {accession} has no XBRL data")
            return None

        instruments, report_date = _extract_debt_instruments(xbrl_instance)

        if not instruments:
            logger.warning(f"No debt instrument data found in XBRL for {accession}")
            return None

        total_current = sum(inst.get("current_portion", 0) for inst in instruments)
        total_noncurrent = sum(inst.get("noncurrent_portion", 0) for inst in instruments)

        logger.info(f"Successfully extracted debt schedule from XBRL: {accession}")
        return {
            "report_date": report_date,
            "debt_instruments": instruments,
            "total_current_debt": total_current,
            "total_noncurrent_debt": total_noncurrent,
        }

    except Exception as e:
        logger.error(f"Failed to extract debt schedule from XBRL: {e}")
        return None


def _extract_debt_instruments(xbrl_instance) -> tuple[list[dict], str | None]:
    """Extract debt instruments from XBRL instance using dimensional data.

    Queries multiple debt concepts and looks for DebtInstrumentAxis dimension
    to get per-instrument breakdown. Falls back to aggregate if no dimensional
    data exists.

    Args:
        xbrl_instance: XBRLInstance object from edgartools

    Returns:
        Tuple of (list of instrument dicts, report_date string or None)
    """
    # Debt concepts to query, grouped by classification
    current_concepts = [
        "DebtCurrent",
        "LongTermDebtCurrent",
        "ShortTermBorrowings",
        "LinesOfCreditCurrent",
        "ConvertibleDebtCurrent",
    ]

    noncurrent_concepts = [
        "DebtNoncurrent",
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "ConvertibleDebtNoncurrent",
        "SecuredDebt",
        "UnsecuredDebt",
    ]

    total_concepts = [
        "DebtInstrumentCarryingAmount",
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    ]

    dim_column = "dim_us-gaap_DebtInstrumentAxis"
    instruments_by_name: dict[str, dict] = {}
    report_date = None

    def _process_concept(concept: str, classification: str):
        """Query a concept and merge results into instruments_by_name.

        Returns True if data was found, False otherwise.
        """
        nonlocal report_date
        try:
            df = xbrl_instance.facts.query().by_concept(concept).to_dataframe()
            if df.empty:
                return False

            # Try to extract report date from the first fact
            if report_date is None and "end" in df.columns:
                end_val = df.iloc[-1].get("end")
                if end_val is not None:
                    report_date = str(end_val)

            if dim_column in df.columns:
                # Dimensional data — per-instrument breakdown
                found_any = False
                for member_name in df[dim_column].dropna().unique():
                    member_df = df[df[dim_column] == member_name]
                    if member_df.empty:
                        continue

                    value = member_df.iloc[-1].get("value")
                    if value is None:
                        continue
                    value = float(value)

                    clean_name = _clean_member_name(str(member_name))
                    if clean_name not in instruments_by_name:
                        instruments_by_name[clean_name] = {
                            "instrument_name": clean_name,
                            "current_portion": 0.0,
                            "noncurrent_portion": 0.0,
                            "maturity_date": None,
                        }

                    if classification == "current":
                        instruments_by_name[clean_name]["current_portion"] += value
                    else:
                        instruments_by_name[clean_name]["noncurrent_portion"] += value
                    found_any = True
                return found_any
            else:
                # No dimensional data — aggregate value
                value = df.iloc[-1].get("value")
                if value is None:
                    return False
                value = float(value)

                agg_name = "Aggregate Debt"
                if agg_name not in instruments_by_name:
                    instruments_by_name[agg_name] = {
                        "instrument_name": agg_name,
                        "current_portion": 0.0,
                        "noncurrent_portion": 0.0,
                        "maturity_date": None,
                    }

                if classification == "current":
                    instruments_by_name[agg_name]["current_portion"] += value
                else:
                    instruments_by_name[agg_name]["noncurrent_portion"] += value
                return True

        except Exception as e:
            logger.debug(f"Could not query concept {concept}: {e}")
            return False

    # Process each concept group using first-match-wins to avoid
    # double-counting from overlapping XBRL concept hierarchies
    # (e.g., DebtNoncurrent is a superset of LongTermDebtNoncurrent).
    for concept in current_concepts:
        if _process_concept(concept, "current"):
            break

    for concept in noncurrent_concepts:
        if _process_concept(concept, "noncurrent"):
            break

    # Only use total concepts if we found nothing so far
    if not instruments_by_name:
        for concept in total_concepts:
            if _process_concept(concept, "noncurrent"):
                break

    # Try to get maturity dates per instrument
    _enrich_maturity_dates(xbrl_instance, instruments_by_name)

    return list(instruments_by_name.values()), report_date


def _enrich_maturity_dates(xbrl_instance, instruments_by_name: dict):
    """Query DebtInstrumentMaturityDate and attach to matching instruments."""
    dim_column = "dim_us-gaap_DebtInstrumentAxis"
    try:
        df = xbrl_instance.facts.query().by_concept("DebtInstrumentMaturityDate").to_dataframe()
        if df.empty or dim_column not in df.columns:
            return

        for member_name in df[dim_column].dropna().unique():
            member_df = df[df[dim_column] == member_name]
            if member_df.empty:
                continue

            date_val = member_df.iloc[-1].get("value")
            if date_val is None:
                continue

            clean_name = _clean_member_name(str(member_name))
            if clean_name in instruments_by_name:
                instruments_by_name[clean_name]["maturity_date"] = str(date_val)

    except Exception as e:
        logger.debug(f"Could not query maturity dates: {e}")


def _clean_member_name(name: str) -> str:
    """Convert XBRL dimension member name to a readable instrument name.

    Examples:
        "us-gaap_TermLoanAMember" → "Term Loan A"
        "vz_FloatingRateNotesMember" → "Floating Rate Notes"
        "TermLoanMember" → "Term Loan"

    Args:
        name: Raw dimension member name from XBRL

    Returns:
        Human-readable instrument name
    """
    # Strip taxonomy prefix (e.g., "us-gaap_", "vz_", "aapl_")
    if "_" in name:
        name = name.split("_", 1)[-1]

    # Strip "Member" suffix
    if name.endswith("Member"):
        name = name[: -len("Member")]

    # Insert spaces before uppercase letters (CamelCase → words)
    result = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    result = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", result)

    return result.strip()
