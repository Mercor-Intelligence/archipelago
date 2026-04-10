"""Fixture loading and querying system for offline mode.

This module provides functions to query fixture data stored in DuckDB.
"""

from datetime import datetime

from utils.db import get_connection


def _query(sql: str, params: list | None = None) -> list[dict]:
    """Execute a query and return results as list of dicts."""
    conn = get_connection()
    cursor = conn.execute(sql, params or [])
    columns = [desc[0] for desc in cursor.description]
    result = cursor.fetchall()
    if not result:
        return []
    return [dict(zip(columns, row)) for row in result]


def _query_one(sql: str, params: list | None = None) -> dict | None:
    """Execute a query and return first result as dict, or None."""
    results = _query(sql, params)
    return results[0] if results else None


_VALID_TABLES = frozenset(
    {
        "bonds",
        "bond_reference",
        "bond_pricing",
        "bond_cashflows",
        "inflation_factors",
        "muni_bonds",
        "muni_reference",
        "muni_pricing",
        "muni_cashflows",
    }
)


def _has_data(table: str) -> bool:
    """Check if a table has any data."""
    if table not in _VALID_TABLES:
        raise ValueError(f"Invalid table name: {table}")
    conn = get_connection()
    result = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return result[0] > 0 if result else False


# ============================================================================
# Government and Corporate Bonds
# ============================================================================


def fixture_search_bonds(
    countries: list[str] | None = None,
    coupon_min: float | None = None,
    coupon_max: float | None = None,
    maturity_date_min: str | None = None,
    maturity_date_max: str | None = None,
    currencies: list[str] | None = None,
    issue_rating_group: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Search bonds from fixtures."""
    if not _has_data("bonds"):
        return []

    conditions = []
    params = []

    if countries:
        placeholders = ", ".join(["?" for _ in countries])
        conditions.append(f"country IN ({placeholders})")
        params.extend(countries)

    if currencies:
        placeholders = ", ".join(["?" for _ in currencies])
        conditions.append(f"currency IN ({placeholders})")
        params.extend(currencies)

    if issue_rating_group:
        conditions.append("issue_rating_group = ?")
        params.append(issue_rating_group)

    if coupon_min is not None:
        conditions.append("coupon >= ?")
        params.append(coupon_min)

    if coupon_max is not None:
        conditions.append("coupon <= ?")
        params.append(coupon_max)

    if maturity_date_min:
        conditions.append("maturity_date >= ?")
        params.append(maturity_date_min)

    if maturity_date_max:
        conditions.append("maturity_date <= ?")
        params.append(maturity_date_max)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT * FROM bonds WHERE {where_clause} LIMIT ?"
    params.append(limit)

    return _query(sql, params)


def fixture_get_bond_reference_data(isins: list[str]) -> list[dict]:
    """Get bond reference data from fixtures."""
    if not isins or not _has_data("bond_reference"):
        return []

    placeholders = ", ".join(["?" for _ in isins])
    sql = f"SELECT * FROM bond_reference WHERE isin IN ({placeholders})"
    return _query(sql, isins)


def fixture_get_bond_pricing_history(
    isin: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Get bond pricing history from fixtures."""
    if not _has_data("bond_pricing"):
        return []

    sql = """
        SELECT * FROM bond_pricing
        WHERE isin = ? AND pricing_date >= ? AND pricing_date <= ?
        ORDER BY pricing_date
    """
    return _query(sql, [isin, start_date, end_date])


def fixture_get_bond_pricing_latest(
    isins: list[str],
    as_of_date: str | None = None,
) -> list[dict]:
    """Get latest bond pricing from fixtures."""
    if not isins or not _has_data("bond_pricing"):
        return []

    results = []
    for isin in isins:
        if as_of_date:
            sql = """
                SELECT * FROM bond_pricing
                WHERE isin = ? AND pricing_date <= ?
                ORDER BY pricing_date DESC LIMIT 1
            """
            row = _query_one(sql, [isin, as_of_date])
        else:
            sql = """
                SELECT * FROM bond_pricing
                WHERE isin = ?
                ORDER BY pricing_date DESC LIMIT 1
            """
            row = _query_one(sql, [isin])

        if row:
            results.append(row)

    return results


def fixture_get_bond_cashflows(isins: list[str]) -> list[dict]:
    """Get bond cashflows from fixtures."""
    if not isins or not _has_data("bond_cashflows"):
        return []

    placeholders = ", ".join(["?" for _ in isins])
    sql = f"SELECT * FROM bond_cashflows WHERE isin IN ({placeholders})"
    return _query(sql, isins)


def fixture_get_inflation_factors(
    country: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Get inflation factors from fixtures."""
    if not _has_data("inflation_factors"):
        return []

    conditions = ["country_code = ?"]
    params: list = [country]

    if start_date:
        conditions.append("factor_date >= ?")
        params.append(start_date)

    if end_date:
        conditions.append("factor_date <= ?")
        params.append(end_date)

    where_clause = " AND ".join(conditions)
    sql = f"SELECT * FROM inflation_factors WHERE {where_clause} ORDER BY factor_date"
    return _query(sql, params)


# ============================================================================
# US Municipal Bonds
# ============================================================================


def fixture_search_municipal_bonds(
    states: list[str] | None = None,
    coupon_min: float | None = None,
    coupon_max: float | None = None,
    maturity_date_min: str | None = None,
    maturity_date_max: str | None = None,
    sectors: list[str] | None = None,
    sources_of_repayment: list[str] | None = None,
    is_insured: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    """Search municipal bonds from fixtures."""
    if not _has_data("muni_bonds"):
        return []

    conditions = []
    params = []

    if states:
        placeholders = ", ".join(["?" for _ in states])
        conditions.append(f"state IN ({placeholders})")
        params.extend(states)

    if sectors:
        placeholders = ", ".join(["?" for _ in sectors])
        conditions.append(f"sector IN ({placeholders})")
        params.extend(sectors)

    if sources_of_repayment:
        placeholders = ", ".join(["?" for _ in sources_of_repayment])
        conditions.append(f"source_of_repayment IN ({placeholders})")
        params.extend(sources_of_repayment)

    if is_insured is not None:
        conditions.append("is_insured = ?")
        params.append(is_insured)

    if coupon_min is not None:
        conditions.append("coupon >= ?")
        params.append(coupon_min)

    if coupon_max is not None:
        conditions.append("coupon <= ?")
        params.append(coupon_max)

    if maturity_date_min:
        conditions.append("maturity_date >= ?")
        params.append(maturity_date_min)

    if maturity_date_max:
        conditions.append("maturity_date <= ?")
        params.append(maturity_date_max)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT * FROM muni_bonds WHERE {where_clause} LIMIT ?"
    params.append(limit)

    return _query(sql, params)


def fixture_get_muni_reference_data(isins: list[str]) -> list[dict]:
    """Get municipal bond reference data from fixtures."""
    if not isins or not _has_data("muni_reference"):
        return []

    placeholders = ", ".join(["?" for _ in isins])
    sql = f"SELECT * FROM muni_reference WHERE isin IN ({placeholders})"
    return _query(sql, isins)


def fixture_get_muni_pricing_latest(
    isins: list[str],
    as_of_date: str | None = None,
) -> list[dict]:
    """Get latest municipal bond pricing from fixtures."""
    if not isins or not _has_data("muni_pricing"):
        return []

    results = []
    for isin in isins:
        if as_of_date:
            sql = """
                SELECT * FROM muni_pricing
                WHERE isin = ? AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT 1
            """
            row = _query_one(sql, [isin, as_of_date])
        else:
            sql = """
                SELECT * FROM muni_pricing
                WHERE isin = ?
                ORDER BY trade_date DESC LIMIT 1
            """
            row = _query_one(sql, [isin])

        if row:
            results.append(row)

    return results


def fixture_get_muni_pricing_history(
    isin: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Get municipal bond pricing history from fixtures."""
    if not _has_data("muni_pricing"):
        return []

    sql = """
        SELECT * FROM muni_pricing
        WHERE isin = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
    """
    return _query(sql, [isin, start_date, end_date])


def fixture_get_muni_pricing_daily_bulk(trade_date: str) -> list[dict]:
    """Get all municipal bond trades for a specific date from fixtures."""
    if not _has_data("muni_pricing"):
        return []

    sql = "SELECT * FROM muni_pricing WHERE trade_date = ?"
    return _query(sql, [trade_date])


# ============================================================================
# Yield Calculation Helpers
# ============================================================================


def _make_yield_error(
    isin: str,
    error: str,
    price: float | None = None,
    settlement_date: str | None = None,
) -> dict:
    """Create an error response for yield calculations."""
    result = {"isin": isin, "error": error}
    if price is not None:
        result["price"] = price
    if settlement_date is not None:
        result["settlement_date"] = settlement_date
    return result


def _years_between(from_date: datetime, to_date: datetime) -> float:
    """Calculate years between two dates."""
    return (to_date - from_date).days / 365.25


def _simple_yield(coupon: float, price: float, redemption: float, years: float) -> float:
    """Calculate simple yield approximation.

    Uses the formula: (coupon + (redemption - price) / years) / ((redemption + price) / 2)
    """
    return (coupon + (redemption - price) / years) / ((redemption + price) / 2)


def _make_yield_dict(yield_value: float) -> dict:
    """Create yield dictionary with all conventions."""
    pct = round(yield_value * 100, 4)
    return {"continuous": pct, "money_market": pct, "semi_annual": pct}


def _calculate_yield_to_call(
    bond: dict,
    settle: datetime,
    coupon: float,
    price: float,
) -> dict | None:
    """Calculate yield to call if bond is callable and call date is in the future."""
    if not (bond.get("callable") and bond.get("call_date") and bond.get("call_price")):
        return None

    call_date_val = bond["call_date"]
    if isinstance(call_date_val, str):
        call_date = datetime.strptime(call_date_val, "%Y-%m-%d")
    else:
        call_date = datetime.combine(call_date_val, datetime.min.time())

    years_to_call = _years_between(settle, call_date)

    if years_to_call <= 0:
        return None

    ytc = _simple_yield(coupon, price, bond["call_price"], years_to_call)
    return _make_yield_dict(ytc)


def fixture_calculate_muni_yield_from_price(
    isin: str,
    price: float,
    settlement_date: str,
) -> dict:
    """Calculate yield from price using fixture data.

    This is a simplified calculation for offline mode.
    """
    if not _has_data("muni_reference"):
        return _make_yield_error(isin, "Fixture data not available")

    sql = "SELECT * FROM muni_reference WHERE isin = ?"
    bond = _query_one(sql, [isin])

    if not bond:
        return _make_yield_error(isin, f"Bond {isin} not found in fixtures")

    maturity_date = bond.get("maturity_date")
    if not maturity_date:
        return _make_yield_error(
            isin, "Missing maturity date for calculation", price, settlement_date
        )

    try:
        settle = datetime.strptime(settlement_date, "%Y-%m-%d")
        if isinstance(maturity_date, str):
            maturity = datetime.strptime(maturity_date, "%Y-%m-%d")
        else:
            maturity = datetime.combine(maturity_date, datetime.min.time())

        years_to_maturity = _years_between(settle, maturity)

        if years_to_maturity <= 0:
            return _make_yield_error(isin, "Bond has matured", price, settlement_date)

        coupon = bond.get("coupon")
        if coupon is None:
            coupon = 4.0
        ytm = _simple_yield(coupon, price, 100.0, years_to_maturity)

        result = {
            "isin": isin,
            "price": price,
            "settlement_date": settlement_date,
            "yield_to_maturity": _make_yield_dict(ytm),
        }

        ytc_dict = _calculate_yield_to_call(bond, settle, coupon, price)
        if ytc_dict:
            result["yield_to_call"] = ytc_dict

        return result

    except (ValueError, TypeError, ZeroDivisionError) as e:
        return _make_yield_error(isin, f"Calculation error: {e}", price, settlement_date)


def fixture_get_muni_cashflows(isins: list[str]) -> list[dict]:
    """Get municipal bond cashflows from fixtures."""
    if not isins or not _has_data("muni_cashflows"):
        return []

    placeholders = ", ".join(["?" for _ in isins])
    sql = f"SELECT * FROM muni_cashflows WHERE isin IN ({placeholders})"
    return _query(sql, isins)
