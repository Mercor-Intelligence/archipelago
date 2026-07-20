"""Utility functions for TaxJar verifiers.

This module provides TaxJar-specific utilities that wrap the generic
verifier utilities. For maximum flexibility, consider using the generic
utilities directly from `generic.py`.
"""

from datetime import UTC, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, TypedDict

from loguru import logger

# Import generic utilities that we'll use internally
from .generic import aggregate_field, find_records_matching, get_nested_field


# Type definitions
class TaxJarOrder(TypedDict):
    """Structure for a TaxJar order transaction."""

    transaction_id: str
    transaction_date: str
    to_country: str
    to_zip: str
    to_state: str
    to_city: str | None
    to_street: str | None
    from_country: str
    from_zip: str | None
    from_state: str | None
    from_city: str | None
    from_street: str | None
    amount: str
    shipping: str
    sales_tax: str
    user_id: int
    provider: str
    customer_id: str | None
    exemption_type: str | None
    transaction_reference_id: str | None
    line_items: list[dict[str, Any]]


class TaxJarRefund(TypedDict):
    """Structure for a TaxJar refund transaction."""

    transaction_id: str
    transaction_date: str
    transaction_reference_id: str | None
    to_country: str
    to_zip: str
    to_state: str
    to_city: str | None
    to_street: str | None
    from_country: str | None
    from_zip: str | None
    from_state: str | None
    from_city: str | None
    from_street: str | None
    amount: str
    shipping: str
    sales_tax: str
    user_id: int
    provider: str
    customer_id: str | None
    exemption_type: str | None
    line_items: list[dict[str, Any]]


class NexusRegion(TypedDict):
    """Structure for a TaxJar nexus region."""

    region_code: str
    country_code: str
    region: str
    country: str


# ====================
# Date Parsing
# ====================


def parse_transaction_date(date_str: str) -> datetime:
    """Parse a transaction date string to datetime.

    Handles YYYY-MM-DD, ISO 8601, and SQLite datetime formats.
    Always returns an offset-aware datetime in UTC to ensure consistent comparisons.

    Args:
        date_str: Date string in YYYY-MM-DD, ISO 8601, or SQLite datetime format

    Returns:
        Offset-aware datetime object in UTC
    """
    if "T" in date_str:
        # ISO 8601 format: 2024-11-12T00:00:00.000Z
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    elif " " in date_str:
        # SQLite datetime format: 2024-11-12 00:00:00
        naive_dt = datetime.strptime(date_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
        return naive_dt.replace(tzinfo=UTC)
    else:
        # Simple date format: 2024-11-12
        # Parse as naive then make offset-aware (UTC) for consistent comparisons
        naive_dt = datetime.strptime(date_str, "%Y-%m-%d")
        return naive_dt.replace(tzinfo=UTC)


def is_date_in_range(date_str: str, from_date: str | None, to_date: str | None) -> bool:
    """Check if a transaction date falls within a date range.

    Args:
        date_str: Transaction date string
        from_date: Start date (inclusive), or None for no lower bound
        to_date: End date (inclusive), or None for no upper bound

    Returns:
        True if date is within range
    """
    txn_date = parse_transaction_date(date_str)

    if from_date:
        start = parse_transaction_date(from_date)
        if txn_date < start:
            return False

    if to_date:
        end = parse_transaction_date(to_date)
        if txn_date > end:
            return False

    return True


# ====================
# Transaction Filtering
# ====================


def filter_transactions_by_date_range(
    transactions: list[dict[str, Any]],
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Filter transactions by date range.

    Args:
        transactions: List of transaction dictionaries with 'transaction_date' field
        from_date: Start date (inclusive), format YYYY-MM-DD
        to_date: End date (inclusive), format YYYY-MM-DD

    Returns:
        Filtered list of transactions within the date range
    """
    if not from_date and not to_date:
        return transactions

    filtered = []
    for txn in transactions:
        date_str = txn.get("transaction_date", "")
        if date_str and is_date_in_range(date_str, from_date, to_date):
            filtered.append(txn)

    logger.info(
        f"Filtered transactions by date range [{from_date} to {to_date}]: "
        f"{len(filtered)} of {len(transactions)}"
    )
    return filtered


def filter_transactions_by_state(
    transactions: list[dict[str, Any]], state_code: str
) -> list[dict[str, Any]]:
    """Filter transactions by destination state.

    Args:
        transactions: List of transaction dictionaries with 'to_state' field
        state_code: Two-letter state code (case-insensitive)

    Returns:
        Filtered list of transactions for the specified state

    Note: Uses find_records_matching() from generic.py (case-insensitive).
    """
    filtered = find_records_matching(transactions, {"to_state": state_code})
    logger.info(
        f"Filtered transactions by state {state_code.upper()}: "
        f"{len(filtered)} of {len(transactions)}"
    )
    return filtered


def filter_transactions_by_zip(
    transactions: list[dict[str, Any]], zip_code: str
) -> list[dict[str, Any]]:
    """Filter transactions by destination ZIP code.

    Args:
        transactions: List of transaction dictionaries with 'to_zip' field
        zip_code: ZIP code to filter by

    Returns:
        Filtered list of transactions for the specified ZIP

    Note: Uses find_records_matching() from generic.py.
    """
    filtered = find_records_matching(transactions, {"to_zip": zip_code})
    logger.info(
        f"Filtered transactions by ZIP {zip_code}: "
        f"{len(filtered)} of {len(transactions)}"
    )
    return filtered


def filter_transactions_by_city(
    transactions: list[dict[str, Any]], city_name: str
) -> list[dict[str, Any]]:
    """Filter transactions by destination city.

    Args:
        transactions: List of transaction dictionaries with 'to_city' field
        city_name: City name to filter by (case-insensitive)

    Returns:
        Filtered list of transactions for the specified city

    Note: Uses find_records_matching() from generic.py (case-insensitive).
    """
    filtered = find_records_matching(transactions, {"to_city": city_name})
    logger.info(
        f"Filtered transactions by city {city_name}: "
        f"{len(filtered)} of {len(transactions)}"
    )
    return filtered


def get_transaction_field(
    transactions: list[dict[str, Any]], transaction_id: str, field_name: str
) -> Any:
    """Get a specific field value from a transaction by ID.

    Args:
        transactions: List of transaction dictionaries
        transaction_id: Transaction ID to find
        field_name: Field name to retrieve (e.g., 'shipping', 'amount', 'sales_tax')
            Supports dot notation for nested fields (e.g., 'line_items.0.product_tax_code')

    Returns:
        Field value if found, None otherwise

    Note: Uses find_transaction_by_id() and get_nested_field() from generic.py internally.
    """
    txn = find_transaction_by_id(transactions, transaction_id)
    if txn is None:
        return None

    # Use generic nested field access
    value = get_nested_field(txn, field_name)

    # Handle field name aliases for backwards compatibility
    if value is None and field_name == "zip":
        value = get_nested_field(txn, "to_zip")
    if value is None and field_name == "city":
        value = get_nested_field(txn, "to_city")

    return value


def get_rate_for_zip(
    rates: list[dict[str, Any]], zip_code: str
) -> dict[str, Any] | None:
    """Get rate record for a specific ZIP code.

    Args:
        rates: List of rate dictionaries
        zip_code: ZIP code to look up (matches 'param_zip' or 'zip' column)

    Returns:
        Rate record dict if found, None otherwise

    Note: Uses find_records_matching() from generic.py internally.
    """
    # Try param_zip first (CSV upload format)
    matches = find_records_matching(rates, {"param_zip": zip_code})
    if matches:
        return matches[0]
    # Try zip (API format)
    matches = find_records_matching(rates, {"zip": zip_code})
    return matches[0] if matches else None


def recalculate_tax_with_rate(
    amount: Decimal, original_rate: Decimal, corrected_rate: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """Recalculate tax using a corrected rate.

    Args:
        amount: Taxable amount
        original_rate: Original tax rate
        corrected_rate: Corrected tax rate

    Returns:
        Tuple of (original_tax, corrected_tax, difference)
    """
    original_tax = amount * original_rate
    corrected_tax = amount * corrected_rate
    difference = corrected_tax - original_tax
    return original_tax, corrected_tax, difference


def filter_transactions_by_exemption_type(
    transactions: list[dict[str, Any]],
    exemption_type: str | None = None,
    exclude_exempt: bool = False,
) -> list[dict[str, Any]]:
    """Filter transactions by exemption type.

    Args:
        transactions: List of transaction dictionaries with 'exemption_type' field
        exemption_type: Specific exemption type to filter for (e.g., 'wholesale')
        exclude_exempt: If True, exclude all exempt transactions

    Returns:
        Filtered list of transactions
    """
    filtered = []
    for txn in transactions:
        txn_exemption = txn.get("exemption_type")

        if exclude_exempt:
            # Exclude all exempt transactions (exemption_type is non-null and non-empty)
            if not txn_exemption or txn_exemption == "non_exempt":
                filtered.append(txn)
        elif exemption_type:
            # Filter for specific exemption type
            if txn_exemption and txn_exemption.lower() == exemption_type.lower():
                filtered.append(txn)
        else:
            # No filter - include all
            filtered.append(txn)

    logger.info(
        f"Filtered transactions by exemption (type={exemption_type}, exclude_exempt={exclude_exempt}): "
        f"{len(filtered)} of {len(transactions)}"
    )
    return filtered


# ====================
# Amount Calculations
# ====================


def sum_transaction_amounts(
    transactions: list[dict[str, Any]], include_shipping: bool = False
) -> Decimal:
    """Sum the amounts of all transactions.

    Args:
        transactions: List of transaction dictionaries with 'amount' and 'shipping' fields
        include_shipping: Whether to include shipping in the total

    Returns:
        Total amount as Decimal

    Note: Uses aggregate_field() from generic.py internally.
    """
    total = aggregate_field(transactions, "amount", "sum")
    if include_shipping:
        total += aggregate_field(transactions, "shipping", "sum")
    return total


def sum_transaction_sales_tax(transactions: list[dict[str, Any]]) -> Decimal:
    """Sum the sales tax of all transactions.

    Args:
        transactions: List of transaction dictionaries with 'sales_tax' field

    Returns:
        Total sales tax as Decimal

    Note: Uses aggregate_field() from generic.py internally.
    """
    return aggregate_field(transactions, "sales_tax", "sum")


# ====================
# Net Calculations
# ====================


def calculate_net_amount(
    orders: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    include_shipping: bool = False,
) -> Decimal:
    """Calculate net amount (orders - refunds).

    Args:
        orders: List of order transactions
        refunds: List of refund transactions
        include_shipping: Whether to include shipping in the calculation

    Returns:
        Net amount as Decimal
    """
    order_total = sum_transaction_amounts(orders, include_shipping)
    refund_total = sum_transaction_amounts(refunds, include_shipping)
    return order_total - refund_total


def calculate_net_sales_tax(
    orders: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
) -> Decimal:
    """Calculate net sales tax (orders - refunds).

    Args:
        orders: List of order transactions
        refunds: List of refund transactions

    Returns:
        Net sales tax as Decimal
    """
    order_tax = sum_transaction_sales_tax(orders)
    refund_tax = sum_transaction_sales_tax(refunds)
    return order_tax - refund_tax


# ====================
# Tax Rate Calculations
# ====================


def calculate_expected_tax(amount: Decimal, rate: Decimal) -> Decimal:
    """Calculate expected tax for an amount at a given rate.

    Args:
        amount: Taxable amount
        rate: Tax rate as decimal (e.g., 0.081 for 8.1%)

    Returns:
        Expected tax amount
    """
    return amount * rate


def calculate_incremental_liability(
    expected_tax: Decimal, collected_tax: Decimal
) -> Decimal:
    """Calculate incremental tax liability (expected - collected).

    Args:
        expected_tax: Tax that should have been collected
        collected_tax: Tax actually collected

    Returns:
        Incremental liability (positive = owes more tax)
    """
    return expected_tax - collected_tax


def calculate_blended_rate(total_tax: Decimal, total_amount: Decimal) -> Decimal:
    """Calculate blended tax rate from total tax and amount.

    Args:
        total_tax: Total tax collected
        total_amount: Total taxable amount

    Returns:
        Blended rate as decimal
    """
    if total_amount == 0:
        return Decimal("0")
    return total_tax / total_amount


# ====================
# Projection Calculations
# ====================


def calculate_projected_amount(base_amount: Decimal, growth_rate: Decimal) -> Decimal:
    """Calculate projected amount with growth rate.

    Args:
        base_amount: Base amount to project from
        growth_rate: Growth rate as decimal (e.g., 0.20 for 20% growth)

    Returns:
        Projected amount
    """
    return base_amount * (Decimal("1") + growth_rate)


def calculate_projected_tax(projected_amount: Decimal, tax_rate: Decimal) -> Decimal:
    """Calculate projected tax for a projected amount.

    Args:
        projected_amount: Projected taxable amount
        tax_rate: Tax rate as decimal

    Returns:
        Projected tax amount
    """
    return projected_amount * tax_rate


# ====================
# Fuzzy Matching
# ====================


def find_transaction_by_id(
    transactions: list[dict[str, Any]], transaction_id: str
) -> dict[str, Any] | None:
    """Find a transaction by ID (exact match).

    Args:
        transactions: List of transaction dictionaries
        transaction_id: Transaction ID to find (matches 'transaction_id' or 'param_transaction_id')

    Returns:
        Transaction dict if found, None otherwise

    Note: Uses find_records_matching() from generic.py internally.
    """
    # Try standard column name first
    matches = find_records_matching(transactions, {"transaction_id": transaction_id})
    if matches:
        return matches[0]
    # Try alternate column name
    matches = find_records_matching(
        transactions, {"param_transaction_id": transaction_id}
    )
    return matches[0] if matches else None


def find_transaction_fuzzy(
    transactions: list[dict[str, Any]], search_term: str, threshold: float = 0.6
) -> dict[str, Any] | None:
    """Find a transaction by fuzzy matching on ID.

    Args:
        transactions: List of transaction dictionaries
        search_term: Search term to match against transaction_id
        threshold: Minimum similarity score (0.0 to 1.0)

    Returns:
        Best matching transaction dict if found above threshold, None otherwise
    """
    # Try exact match first
    for txn in transactions:
        if txn.get("transaction_id") == search_term:
            return txn

    # Fuzzy match
    best_match = None
    best_score = threshold

    for txn in transactions:
        txn_id = txn.get("transaction_id", "")
        score = SequenceMatcher(None, txn_id.lower(), search_term.lower()).ratio()
        if score > best_score:
            best_score = score
            best_match = txn

    if best_match:
        logger.info(
            f"Fuzzy matched transaction '{search_term}' with score {best_score:.2f}"
        )

    return best_match


def find_nexus_region(
    regions: list[dict[str, Any]], region_code: str
) -> dict[str, Any] | None:
    """Find a nexus region by region code.

    Args:
        regions: List of nexus region dictionaries
        region_code: Two-letter region code (case-insensitive)

    Returns:
        Region dict if found, None otherwise

    Note: Uses find_records_matching() from generic.py (case-insensitive).
    """
    matches = find_records_matching(regions, {"region_code": region_code})
    return matches[0] if matches else None


# ====================
# Quarter Helpers
# ====================


def get_quarter_date_range(year: int, quarter: int) -> tuple[str, str]:
    """Get date range for a fiscal quarter.

    Args:
        year: Year (e.g., 2025)
        quarter: Quarter number (1-4)

    Returns:
        Tuple of (from_date, to_date) in YYYY-MM-DD format

    Raises:
        ValueError: If quarter is not 1, 2, 3, or 4
    """
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"Invalid quarter: {quarter}. Must be 1, 2, 3, or 4.")

    quarter_starts = {
        1: (1, 1),  # Jan 1
        2: (4, 1),  # Apr 1
        3: (7, 1),  # Jul 1
        4: (10, 1),  # Oct 1
    }
    quarter_ends = {
        1: (3, 31),  # Mar 31
        2: (6, 30),  # Jun 30
        3: (9, 30),  # Sep 30
        4: (12, 31),  # Dec 31
    }

    start_month, start_day = quarter_starts[quarter]
    end_month, end_day = quarter_ends[quarter]

    from_date = f"{year}-{start_month:02d}-{start_day:02d}"
    to_date = f"{year}-{end_month:02d}-{end_day:02d}"

    return from_date, to_date
