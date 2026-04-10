"""Bank transactions resource implementation for offline provider."""

import copy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import BankTransaction
from mcp_servers.xero.db.session import async_session


async def get_bank_transactions(
    self,
    where: str | None = None,
    unitdp: int | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Get bank transactions from database with filtering and pagination.

    Args:
        where: Filter expression (e.g., 'Type=="RECEIVE"')
        unitdp: Decimal places for unit amounts (2 or 4)
        page: Page number (1-indexed)

    Returns:
        Dictionary with BankTransactions array and metadata

    Reference:
        XER-14_BankTransactions_API_Reference.md
    """
    async with async_session() as session:
        result = await session.execute(select(BankTransaction))
        transactions = result.scalars().all()

        # Convert to dict format
        transactions_data = [txn.to_dict() for txn in transactions]

    # Apply where clause filter (basic implementation for common patterns)
    if where:
        logger.info(f"Applying where filter to bank transactions: {where}")
        logger.debug(f"Transactions before filter: {len(transactions_data)}")
        transactions_data = _apply_where_filter(transactions_data, where)
        logger.info(f"Transactions after filter: {len(transactions_data)}")

    # Validate totals consistency and log warnings (PR #3 pattern)
    for transaction in transactions_data:
        line_items = transaction.get("LineItems", [])
        if line_items:
            # Calculate sum of line amounts using Decimal for precision
            line_total = sum(Decimal(str(item.get("LineAmount", 0))) for item in line_items)
            transaction_total = Decimal(str(transaction.get("SubTotal", 0)))

            # Check consistency within ±0.01 tolerance
            if abs(line_total - transaction_total) > Decimal("0.01"):
                logger.warning(
                    f"BankTransaction {transaction.get('BankTransactionID', 'Unknown')} "
                    f"totals inconsistency: LineItems sum={line_total:.2f}, "
                    f"SubTotal={transaction_total:.2f}, "
                    f"difference={abs(line_total - transaction_total):.2f}"
                )

    # Apply unitdp formatting if specified
    if unitdp is not None:
        transactions_data = _apply_unitdp_formatting(transactions_data, unitdp)

    # Apply pagination (page size = 100, matching Xero API default)
    page_size = 100
    total_count = len(transactions_data)
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0

    if page:
        # Paginate results
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        transactions_data = transactions_data[start_idx:end_idx]
        current_page = page
    else:
        # No pagination - return first page
        transactions_data = transactions_data[:page_size]
        current_page = 1

    has_next = current_page < total_pages

    # Build response with metadata
    response = {
        "BankTransactions": transactions_data,
        "meta": {
            "mode": "offline",
            "provider": "xero-mock",
            "page": current_page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_next": has_next,
            "calledAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        },
    }

    return response


def _apply_where_filter(transactions: list[dict], where: str) -> list[dict]:
    """Apply basic where clause filtering.

    Supports simple equality checks like:
    - Type=="RECEIVE"
    - Status=="AUTHORISED"
    - Type=="SPEND" AND Status=="AUTHORISED"

    Args:
        transactions: List of bank transaction dictionaries
        where: Where clause expression

    Returns:
        Filtered list of bank transactions
    """
    if "==" not in where:
        inferred = _infer_bank_transaction_field(where.strip())
        if inferred:
            logger.info(f"Inferred where clause: {where} → {inferred}")
            where = inferred
        else:
            logger.warning(f"Unsupported where clause format (no ==): {where}")
            return transactions

    try:
        if " AND " in where:
            logger.debug("Handling AND condition in where clause")
            conditions = where.split(" AND ")
            for condition in conditions:
                transactions = _apply_single_where_condition(transactions, condition.strip())
            return transactions
        else:
            return _apply_single_where_condition(transactions, where)

    except Exception as e:
        logger.warning(f"Failed to parse where clause '{where}': {e}")
        return transactions


def _apply_single_where_condition(transactions: list[dict], condition: str) -> list[dict]:
    """Apply a single where condition.

    Args:
        transactions: List of transactions
        condition: Single condition like Type=="RECEIVE"

    Returns:
        Filtered transactions
    """
    if "==" not in condition:
        inferred = _infer_bank_transaction_field(condition.strip())
        if inferred:
            logger.info(f"Inferred where clause: {condition} → {inferred}")
            condition = inferred
        else:
            logger.warning(f"Unsupported where clause format (no ==): {condition}")
            return transactions

    parts = condition.split("==", 1)
    if len(parts) != 2:
        return transactions

    field = parts[0].strip()
    value = parts[1].strip().strip('"').strip("'")

    logger.debug(f"Filtering bank transactions by {field}=={value}")

    if "." in field:
        field_parts = field.split(".", 1)
        if len(field_parts) == 2:
            parent, child = field_parts
            filtered = [
                txn
                for txn in transactions
                if parent in txn
                and isinstance(txn[parent], dict)
                and txn[parent].get(child) == value
            ]
            logger.debug(
                f"Matched {len(filtered)} transactions out of {len(transactions)} (nested field)"
            )
            return filtered
    else:
        filtered = [txn for txn in transactions if txn.get(field) == value]
        logger.debug(f"Matched {len(filtered)} transactions out of {len(transactions)}")
        return filtered

    return transactions


def _apply_unitdp_formatting(transactions: list[dict], unitdp: int) -> list[dict]:
    """Apply unitdp formatting to UnitAmount fields in line items.

    Args:
        transactions: List of bank transaction dictionaries
        unitdp: Decimal places (2 or 4)

    Returns:
        Transactions with formatted UnitAmount values
    """
    # Create a deep copy to avoid modifying original data
    formatted_transactions = copy.deepcopy(transactions)

    for transaction in formatted_transactions:
        line_items = transaction.get("LineItems", [])
        for item in line_items:
            if "UnitAmount" in item:
                # Round UnitAmount to specified decimal places using Decimal for precision
                unit_amount = Decimal(str(item["UnitAmount"]))
                item["UnitAmount"] = float(round(unit_amount, unitdp))

    return formatted_transactions


def _infer_bank_transaction_field(value: str) -> str | None:
    """Infer the field name from common bank transaction values for GUI compatibility.

    The GUI sends shorthand values like "RECEIVE" instead of full where clauses
    like 'Type=="RECEIVE"'. This function maps common values to their fields.

    Args:
        value: The value to infer from (e.g., "RECEIVE", "SPEND", "AUTHORISED")

    Returns:
        Full where clause or None if cannot infer

    Examples:
        "RECEIVE" → 'Type=="RECEIVE"'
        "SPEND" → 'Type=="SPEND"'
        "AUTHORISED" → 'Status=="AUTHORISED"'
        "DRAFT" → 'Status=="DRAFT"'
    """
    value_upper = value.upper()

    if value_upper in [
        "RECEIVE",
        "SPEND",
        "RECEIVE-OVERPAYMENT",
        "RECEIVE-PREPAYMENT",
        "SPEND-OVERPAYMENT",
        "SPEND-PREPAYMENT",
        "RECEIVE-TRANSFER",
        "SPEND-TRANSFER",
    ]:
        return f'Type=="{value_upper}"'

    if value_upper in ["AUTHORISED", "DELETED", "VOIDED", "DRAFT"]:
        return f'Status=="{value_upper}"'

    return None
