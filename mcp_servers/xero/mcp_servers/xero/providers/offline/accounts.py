"""Accounts resource implementation for offline provider."""

from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models import Account
from mcp_servers.xero.db.session import async_session


async def get_accounts(
    self, where: str | None = None, order: str | None = None, page: int | None = None
) -> dict[str, Any]:
    """Get chart of accounts from database with filtering support.

    Args:
        where: Filter expression (e.g., 'Class=="ASSET"', 'Type=="BANK"')
        order: Order by field (not yet implemented)
        page: Page number for pagination (not yet implemented)

    Returns:
        Dictionary with Accounts array and metadata
    """
    async with async_session() as session:
        result = await session.execute(select(Account))
        accounts = result.scalars().all()

        # Convert to dict format
        accounts_data = [account.to_dict() for account in accounts]

    # Apply where clause filter if provided
    if where:
        logger.debug(f"Applying where filter to accounts: {where}")
        accounts_data = _apply_where_filter(accounts_data, where)
        logger.debug(f"Filtered accounts count: {len(accounts_data)}")

    response = {"Accounts": accounts_data}
    return self._add_metadata(response, "xero-mock", "offline")


def _apply_where_filter(accounts: list[dict], where: str) -> list[dict]:
    """Apply basic where clause filtering to accounts.

    Supports simple equality checks like:
    - Class=="ASSET"
    - Type=="BANK"
    - Status=="ACTIVE"
    - Class=="ASSET" AND Type=="BANK"

    Also supports GUI-friendly shorthand (infers field from value):
    - "ASSET" → Class=="ASSET"
    - "BANK" → Type=="BANK"
    - "ACTIVE" → Status=="ACTIVE"

    Args:
        accounts: List of account dictionaries
        where: Where clause expression

    Returns:
        Filtered list of accounts
    """
    if "==" not in where:
        inferred = _infer_account_field(where.strip())
        if inferred:
            logger.info(f"Inferred where clause: {where} → {inferred}")
            where = inferred
        else:
            logger.warning(f"Unsupported where clause format (no ==): {where}")
            return accounts

    try:
        if " AND " in where:
            conditions = where.split(" AND ")
            for condition in conditions:
                accounts = _apply_single_where_condition(accounts, condition.strip())
            return accounts
        else:
            return _apply_single_where_condition(accounts, where)

    except Exception as e:
        logger.warning(f"Failed to parse where clause '{where}': {e}")
        return accounts


def _apply_single_where_condition(accounts: list[dict], condition: str) -> list[dict]:
    """Apply a single where condition to accounts.

    Args:
        accounts: List of accounts
        condition: Single condition like Class=="ASSET"

    Returns:
        Filtered accounts
    """
    if "==" not in condition:
        inferred = _infer_account_field(condition.strip())
        if inferred:
            logger.info(f"Inferred where clause: {condition} → {inferred}")
            condition = inferred
        else:
            logger.warning(f"Unsupported where clause format (no ==): {condition}")
            return accounts

    parts = condition.split("==", 1)
    if len(parts) != 2:
        return accounts

    field = parts[0].strip()
    value = parts[1].strip().strip('"').strip("'")

    logger.debug(f"Filtering accounts by {field}=={value}")

    filtered = [acc for acc in accounts if acc.get(field) == value]
    logger.debug(f"Matched {len(filtered)} accounts out of {len(accounts)}")

    return filtered


def _infer_account_field(value: str) -> str | None:
    """Infer the field name from common account values for GUI compatibility.

    The GUI sends shorthand values like "ASSET" instead of full where clauses
    like 'Class=="ASSET"'. This function maps common values to their fields.

    Args:
        value: The value to infer from (e.g., "ASSET", "BANK", "ACTIVE")

    Returns:
        Full where clause or None if cannot infer

    Examples:
        "ASSET" → 'Class=="ASSET"'
        "BANK" → 'Type=="BANK"'
        "ACTIVE" → 'Status=="ACTIVE"'
    """
    value_upper = value.upper()

    if value_upper in ["ASSET", "LIABILITY", "REVENUE", "EXPENSE", "EQUITY"]:
        return f'Class=="{value_upper}"'

    if value_upper in [
        "BANK",
        "CURRENT",
        "CURRLIAB",
        "DEPRECIATN",
        "DIRECTCOSTS",
        "FIXED",
        "INVENTORY",
        "NONCURRLIAB",
        "OTHERINCOME",
        "OVERHEADS",
        "PREPAYMENT",
        "SALES",
        "TERMLIAB",
        "PAYGLIABILITY",
        "SUPERANNUATIONEXPENSE",
        "SUPERANNUATIONLIABILITY",
        "WAGESEXPENSE",
        "WAGESPAYABLELIABILITY",
    ]:
        return f'Type=="{value_upper}"'

    if value_upper in ["ACTIVE", "ARCHIVED", "DELETED"]:
        return f'Status=="{value_upper}"'

    if value_upper in [
        "NONE",
        "INPUT",
        "OUTPUT",
        "EXEMPTINPUT",
        "EXEMPTOUTPUT",
        "INPUTTAXED",
        "BASEXCLUDED",
        "GSTONCAPIMPORTS",
        "GSTONIMPORTS",
        "ZERORATEDINPUT",
        "ZERORATEDOUTPUT",
    ]:
        return f'TaxType=="{value_upper}"'

    return None
