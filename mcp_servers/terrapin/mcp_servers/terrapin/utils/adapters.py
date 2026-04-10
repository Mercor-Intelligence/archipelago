"""Response adapters for API-compatible offline mode output.

Transforms fixture query results (list[dict] with datetime.date objects)
into serialized records with ISO date strings.
"""

from datetime import date, datetime
from typing import Any


def _serialize_value(value: Any) -> Any:
    """Convert Python objects to JSON-serializable values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return value


def _serialize_records(records: list[dict]) -> list[dict]:
    """Convert all date objects in records to ISO strings."""
    return [_serialize_value(record) for record in records]


def adapt_bond_search(results: list[dict]) -> list[dict]:
    """Adapt bond search results."""
    return _serialize_records(results)


def adapt_muni_search(results: list[dict]) -> list[dict]:
    """Adapt municipal bond search results."""
    return _serialize_records(results)


def adapt_bond_reference(results: list[dict]) -> list[dict]:
    """Adapt bond reference data results.

    Transforms fixture field names to match model:
    - issuer_lei -> lei
    - issuer_country -> country_code
    """
    adapted = []
    for record in results:
        record = dict(record)  # Copy to avoid mutating original
        if "issuer_lei" in record:
            record["lei"] = record.pop("issuer_lei")
        if "issuer_country" in record:
            record["country_code"] = record.pop("issuer_country")
        adapted.append(_serialize_value(record))
    return adapted


def adapt_muni_reference(results: list[dict]) -> list[dict]:
    """Adapt municipal reference data results."""
    return _serialize_records(results)


def adapt_bond_pricing(results: list[dict]) -> list[dict]:
    """Adapt bond pricing results (history or latest)."""
    return _serialize_records(results)


def adapt_muni_pricing(results: list[dict]) -> list[dict]:
    """Adapt municipal pricing results."""
    return _serialize_records(results)


def adapt_bond_cashflows(results: list[dict]) -> list[dict]:
    """Adapt bond cashflow results.

    Transforms flat fixture records into nested structure matching API response:
    - Groups records by ISIN
    - Converts payment_date -> date, payment_amount -> amount, payment_type -> type
    """
    from collections import defaultdict

    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in results:
        isin = record.get("isin")
        if isin:
            grouped[isin].append(
                {
                    "type": record.get("payment_type"),
                    "date": _serialize_value(record.get("payment_date")),
                    "amount": record.get("payment_amount"),
                }
            )

    return [{"isin": isin, "cashflows": cfs} for isin, cfs in grouped.items()]


def adapt_muni_cashflows(results: list[dict]) -> list[dict]:
    """Adapt municipal cashflow results.

    Transforms flat fixture records into nested structure matching API response:
    - Groups records by ISIN
    - Converts payment_date -> date, payment_amount -> amount, payment_type -> type
    """
    from collections import defaultdict

    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in results:
        isin = record.get("isin")
        if isin:
            grouped[isin].append(
                {
                    "type": record.get("payment_type"),
                    "date": _serialize_value(record.get("payment_date")),
                    "amount": record.get("payment_amount"),
                }
            )

    return [{"isin": isin, "cashflows": cfs} for isin, cfs in grouped.items()]


def adapt_inflation_factors(results: list[dict]) -> list[dict]:
    """Adapt inflation factor results.

    Transforms fixture field names to match API response format:
    - factor_date -> date
    - inflation_factor -> factor
    - country_code is dropped (not in API response)

    Records with null factor_date are skipped since date is a required field.
    """
    adapted = []
    for record in results:
        factor_date = record.get("factor_date")
        if factor_date is None:
            continue  # Skip records without required date field
        adapted.append(
            {
                "date": _serialize_value(factor_date),
                "factor": record.get("inflation_factor"),
                "type": record.get("type", "BLS"),
            }
        )
    return adapted


def adapt_muni_yield(result: dict | None) -> dict | None:
    """Adapt municipal yield calculation result."""
    if result is None:
        return None
    return _serialize_value(result)
