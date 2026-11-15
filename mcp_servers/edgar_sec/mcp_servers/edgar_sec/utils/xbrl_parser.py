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


_AWARD_TYPE_KEY = "dim_us-gaap_AwardTypeAxis"


def _measure_semantics(measure: object) -> str | None:
    """Canonical semantics for one XBRL unit measure."""
    text = str(measure or "").strip().lower()
    local_name = re.split(r"[:}]", text)[-1]
    if local_name in {"share", "shares"}:
        return "shares"
    if text.startswith("iso4217:") or "iso4217}" in text:
        return "currency"
    return None


def _unit_ref_semantics(unit_ref: object) -> str | None:
    """Best-effort semantics for legacy/mocked facts without a unit registry."""
    normalized = re.sub(r"[^a-z0-9]", "", str(unit_ref or "").lower())
    currency_markers = ("usd", "dollar", "eur", "euro", "gbp", "pound", "currency")
    if any(marker in normalized for marker in currency_markers):
        return "currency"
    if normalized in {"share", "shares"} or normalized.endswith("shares"):
        return "shares"
    return None


def _unit_semantics(xbrl_instance, unit_ref: object) -> str | None:
    """Resolve a filer-defined unit ID through the XBRL unit registry."""
    units = getattr(xbrl_instance, "units", None)
    unit = units.get(unit_ref) if isinstance(units, dict) else None
    if unit is None:
        return _unit_ref_semantics(unit_ref)

    if isinstance(unit, dict):
        unit_type = unit.get("type")
        measure = unit.get("measure")
        numerator = unit.get("numerator") or []
        denominator = unit.get("denominator") or []
    else:
        unit_type = getattr(unit, "type", None)
        measure = getattr(unit, "measure", None)
        numerator = getattr(unit, "numerator", None) or []
        denominator = getattr(unit, "denominator", None) or []

    if unit_type == "divide":
        numerator_kinds = {_measure_semantics(item) for item in numerator}
        denominator_kinds = {_measure_semantics(item) for item in denominator}
        if "currency" in numerator_kinds and "shares" in denominator_kinds:
            return "currency"
        return None
    return _measure_semantics(measure)


def _concept_facts(xbrl_instance, concept: str, expected_unit_ref: str | None = None) -> list[dict]:
    """Facts for one XBRL concept, keeping their dimensions and periods.

    Uses execute() rather than to_dataframe(): the dataframe projection drops
    the dim_* columns, so award-type filtering is impossible on it. Edgartools'
    concept query is fuzzy, so results are filtered back to the exact requested
    US-GAAP concept and, when supplied, its expected unit.
    """
    try:
        facts = xbrl_instance.facts.query().by_concept(concept).execute()
    except Exception as e:
        logger.debug(f"Could not query concept {concept}: {e}")
        return []
    dict_facts = [fact for fact in (facts or []) if isinstance(fact, dict)]
    exact_concepts = {concept, f"us-gaap:{concept}"}
    unit_semantics = {
        str(fact.get("unit_ref")): _unit_semantics(xbrl_instance, fact.get("unit_ref"))
        for fact in dict_facts
    }
    filtered = [
        fact
        for fact in dict_facts
        if fact.get("concept") in exact_concepts
        and (
            expected_unit_ref is None
            or unit_semantics.get(str(fact.get("unit_ref"))) == expected_unit_ref
        )
    ]
    return filtered


def _fact_period(fact: dict) -> str:
    """Period a fact belongs to: its instant for balances, end date for activity."""
    for key in ("period_instant", "period_end", "period_key"):
        value = fact.get(key)
        if value:
            return str(value)
    return ""


def _fact_value(fact: dict) -> int | None:
    """Fact value as a share count, or None when it is not numeric."""
    value = fact.get("value")
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _fact_float_value(fact: dict) -> float | None:
    """Fact value as a float, or None when it is not numeric."""
    value = fact.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_dimensions(fact: dict) -> bool:
    return any(key.startswith("dim_") for key in fact)


def _prefer_undimensioned(facts: list[dict]) -> list[dict]:
    """Keep the consolidated facts, which filers report without dimensions.

    Falls back to the dimensioned facts when a filer only tags per-plan or
    per-award-type figures, so nothing is lost when no total is published.
    """
    undimensioned = [fact for fact in facts if not _has_dimensions(fact)]
    selected = undimensioned or facts
    return selected


def _award_type_facts(
    facts: list[dict],
    member: str,
    exclude: str | None = None,
    require_axis: bool = False,
) -> list[dict]:
    """Facts whose award type matches member (case-insensitive substring).

    When no fact carries an award-type axis the filer did not split by award
    type: consolidated facts are returned instead, unless require_axis says
    this award class only exists as a dimension (PSUs).
    """
    if not any(_AWARD_TYPE_KEY in fact for fact in facts):
        return [] if require_axis else _prefer_undimensioned(facts)

    matched = []
    for fact in facts:
        award_type = str(fact.get(_AWARD_TYPE_KEY) or "").lower()
        if member.lower() not in award_type:
            continue
        if exclude and exclude.lower() in award_type:
            continue
        matched.append(fact)
    return matched


def _values_in_period_order(facts: list[dict]) -> list[tuple[str, int | None]]:
    """Period totals ordered oldest first, skipping ambiguous fact groups."""
    by_period: dict[str, list[tuple[tuple[tuple[str, str], ...], int]]] = {}
    for fact in facts:
        period = _fact_period(fact)
        value = _fact_value(fact)
        if not period or value is None:
            continue
        dimensions = tuple(
            sorted((key, str(member)) for key, member in fact.items() if key.startswith("dim_"))
        )
        by_period.setdefault(period, []).append((dimensions, value))

    pairs = []
    for period in sorted(by_period):
        values_by_dimensions: dict[tuple[tuple[str, str], ...], int] = {}
        ambiguous = False
        for dimensions, value in by_period[period]:
            if dimensions in values_by_dimensions and values_by_dimensions[dimensions] != value:
                ambiguous = True
                break
            values_by_dimensions[dimensions] = value
        if ambiguous:
            pairs.append((period, None))
            continue

        dimension_sets = {
            tuple(key for key, _member in dimensions) for dimensions in values_by_dimensions
        }
        if dimension_sets and () not in dimension_sets and len(dimension_sets) == 1:
            pairs.append((period, sum(values_by_dimensions.values())))
            continue

        distinct_values = set(values_by_dimensions.values())
        if len(distinct_values) == 1:
            pairs.append((period, distinct_values.pop()))
        else:
            pairs.append((period, None))
    return pairs


def _latest_period_value(facts: list[dict]) -> int | None:
    """Value of the fact from the most recent period."""
    pairs = _values_in_period_order(facts)
    return pairs[-1][1] if pairs else None


def _latest_period_float_value(facts: list[dict]) -> float | None:
    """Latest float when all facts in that period agree; otherwise None."""
    by_period: dict[str, list[float]] = {}
    for fact in facts:
        period = _fact_period(fact)
        value = _fact_float_value(fact)
        if period and value is not None:
            by_period.setdefault(period, []).append(value)
    if not by_period:
        return None

    values = set(by_period[max(by_period)])
    return values.pop() if len(values) == 1 else None


def _balance_bounds(facts: list[dict]) -> tuple[int | None, int | None]:
    """Opening and closing balances for an instant (point-in-time) concept.

    Filings tag these balances at each period boundary, so the newest instant
    is the reported closing balance and the one immediately before it opens the
    period the activity belongs to. A 10-K also tags older comparative years,
    which must not be mistaken for the opening balance. The closing balance is
    None when only one instant is present, since there is nothing to close to.
    """
    by_period = {}
    for period, value in _values_in_period_order(facts):
        by_period[period] = value

    periods = sorted(by_period)
    if not periods:
        return None, None
    if len(periods) < 2:
        return by_period[periods[0]], None
    return by_period[periods[-2]], by_period[periods[-1]]


def _close_roll_forward(
    beginning: int | None,
    additions: list[int],
    reductions: list[int],
) -> int | None:
    """Close a share roll-forward, or return None when it cannot be trusted.

    Returns None when no movement facts were found (echoing the opening
    balance would assert an unchanged position the filing never reported) and
    when the arithmetic lands below zero (proof the facts came from mismatched
    contexts, e.g. an opening balance paired with another period's activity).
    """
    if beginning is None or (not additions and not reductions):
        return None

    ending = beginning + sum(additions) - sum(abs(value) for value in reductions)
    return ending if ending >= 0 else None


def _extract_stock_options(xbrl_instance) -> dict | None:
    """Extract stock option activity from XBRL instance.

    Args:
        xbrl_instance: XBRLInstance object from edgar tools

    Returns:
        Dictionary with stock option data or None
    """
    try:
        balance_concept = (
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber"  # noqa: E501
        )
        movement_concepts = {
            "granted": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsGrantsInPeriodGross",  # noqa: E501
            "exercised": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsExercisesInPeriod",  # noqa: E501
            "forfeited": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsForfeituresInPeriod",  # noqa: E501
            "expired": "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsExpirationsInPeriod",  # noqa: E501
        }

        result = {}

        balance_candidates = _concept_facts(
            xbrl_instance, balance_concept, expected_unit_ref="shares"
        )
        balance_facts = _prefer_undimensioned(balance_candidates)
        beginning, reported_ending = _balance_bounds(balance_facts)
        if beginning is not None:
            result["outstanding_beginning"] = beginning

        for field, concept in movement_concepts.items():
            movement_candidates = _concept_facts(xbrl_instance, concept, expected_unit_ref="shares")
            movement_facts = _prefer_undimensioned(movement_candidates)
            value = _latest_period_value(movement_facts)
            if field == "exercised" and value is None:
                fallback_facts = _prefer_undimensioned(
                    _concept_facts(
                        xbrl_instance,
                        "StockIssuedDuringPeriodSharesStockOptionsExercised",
                        expected_unit_ref="shares",
                    )
                )
                value = _latest_period_value(fallback_facts)
            if value is not None:
                result[field] = value

        exercise_price_facts = _concept_facts(
            xbrl_instance,
            "ShareBasedCompensationArrangementsByShareBasedPaymentAward"
            "OptionsExercisesInPeriodWeightedAverageExercisePrice",
            expected_unit_ref="currency",
        )
        exercise_price = _latest_period_float_value(_prefer_undimensioned(exercise_price_facts))
        if exercise_price is not None:
            result["weighted_avg_exercise_price_exercised"] = exercise_price

        outstanding_ending = reported_ending
        if outstanding_ending is None:
            outstanding_ending = _close_roll_forward(
                result.get("outstanding_beginning"),
                additions=[v for v in [result.get("granted")] if v is not None],
                reductions=[
                    v
                    for v in [
                        result.get("exercised"),
                        result.get("forfeited"),
                        result.get("expired"),
                    ]
                    if v is not None
                ],
            )
        if outstanding_ending is not None:
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

        balance_concept = f"{base_concept}Number"
        movement_concepts = {
            "granted": f"{base_concept}GrantsInPeriod",
            "vested": f"{base_concept}VestedInPeriod",
            "forfeited": f"{base_concept}ForfeitedInPeriod",
        }

        def _rsu_facts(concept: str) -> list[dict]:
            """RSU facts for a concept, or consolidated facts when unsplit."""
            return _award_type_facts(
                _concept_facts(xbrl_instance, concept, expected_unit_ref="shares"),
                member="RestrictedStockUnitsRSUMember",
            )

        result = {}

        beginning, reported_ending = _balance_bounds(_rsu_facts(balance_concept))
        if beginning is not None:
            result["unvested_beginning"] = beginning

        for field, concept in movement_concepts.items():
            value = _latest_period_value(_rsu_facts(concept))
            if value is not None:
                result[field] = value

        unvested_ending = reported_ending
        if unvested_ending is None:
            unvested_ending = _close_roll_forward(
                result.get("unvested_beginning"),
                additions=[v for v in [result.get("granted")] if v is not None],
                reductions=[
                    v for v in [result.get("vested"), result.get("forfeited")] if v is not None
                ],
            )
        if unvested_ending is not None:
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

        balance_concept = f"{base_concept}Number"
        movement_concepts = {
            "granted": f"{base_concept}GrantsInPeriod",
            "vested": f"{base_concept}VestedInPeriod",
            "forfeited": f"{base_concept}ForfeitedInPeriod",
        }

        def _psu_facts(concept: str) -> list[dict]:
            """PSU facts for a concept (PSU award members name Performance).

            PSUs only exist as an award-type dimension, so an unsplit filing
            has no PSU figures to report.
            """
            return _award_type_facts(
                _concept_facts(xbrl_instance, concept, expected_unit_ref="shares"),
                member="Performance",
                exclude="RestrictedStockUnitsRSUMember",
                require_axis=True,
            )

        result = {}

        beginning, reported_ending = _balance_bounds(_psu_facts(balance_concept))
        if beginning is not None:
            result["unvested_beginning"] = beginning

        for field, concept in movement_concepts.items():
            value = _latest_period_value(_psu_facts(concept))
            if value is not None:
                result[field] = value

        unvested_ending = reported_ending
        if unvested_ending is None:
            unvested_ending = _close_roll_forward(
                result.get("unvested_beginning"),
                additions=[v for v in [result.get("granted")] if v is not None],
                reductions=[
                    v for v in [result.get("vested"), result.get("forfeited")] if v is not None
                ],
            )
        if unvested_ending is not None:
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
