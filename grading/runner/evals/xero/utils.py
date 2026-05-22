"""Utility functions for Xero verifiers."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def find_by_id(
    records: list[dict[str, Any]], id_field: str, id_value: str
) -> dict[str, Any] | None:
    """Find a record by ID field."""
    for record in records:
        if str(record.get(id_field, "")).lower() == str(id_value).lower():
            return record
    return None


def find_by_code(records: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    """Find an account by Code field."""
    for record in records:
        if str(record.get("Code", "")).lower() == str(code).lower():
            return record
    return None


def find_by_name(
    records: list[dict[str, Any]], name: str, name_field: str = "Name"
) -> dict[str, Any] | None:
    """Find a record by Name field (case-insensitive)."""
    for record in records:
        if str(record.get(name_field, "")).lower() == str(name).lower():
            return record
    return None


def find_by_field(
    records: list[dict[str, Any]], field: str, value: str
) -> dict[str, Any] | None:
    """Find a record where field matches value (case-insensitive)."""
    for record in records:
        record_value = get_nested_field(record, field)
        if record_value is not None and str(record_value).lower() == str(value).lower():
            return record
    return None


def get_nested_field(obj: dict[str, Any] | None, path: str) -> Any:
    """Get a nested field using dot notation (e.g., 'LineItems.0.AccountCode')."""
    if obj is None or not path:
        return None

    parts = path.split(".")
    current = obj

    for part in parts:
        if current is None:
            return None
        if isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            except ValueError:
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None

    return current


def sum_field(records: list[dict[str, Any]], field: str) -> Decimal:
    """Sum a numeric field across records."""
    total = Decimal("0")
    for record in records:
        value = get_nested_field(record, field)
        if value is not None:
            try:
                total += Decimal(str(value))
            except Exception:
                pass
    return total


def filter_by_type(
    transactions: list[dict[str, Any]], tx_type: str
) -> list[dict[str, Any]]:
    """Filter bank transactions by Type (RECEIVE/SPEND)."""
    return [t for t in transactions if t.get("Type", "").upper() == tx_type.upper()]


def filter_by_account_code(
    transactions: list[dict[str, Any]], account_code: str
) -> list[dict[str, Any]]:
    """Filter transactions where any line item uses the given account code."""
    result = []
    for txn in transactions:
        line_items = txn.get("LineItems", [])
        for item in line_items:
            if str(item.get("AccountCode", "")).lower() == str(account_code).lower():
                result.append(txn)
                break
    return result


def filter_by_account_class(
    accounts: list[dict[str, Any]], account_class: str
) -> list[dict[str, Any]]:
    """Filter accounts by Class (ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE)."""
    return [a for a in accounts if a.get("Class", "").upper() == account_class.upper()]


def sum_transactions_by_account(
    transactions: list[dict[str, Any]], account_code: str
) -> Decimal:
    """Sum transaction amounts for a specific account code."""
    total = Decimal("0")
    for txn in transactions:
        for item in txn.get("LineItems", []):
            if str(item.get("AccountCode", "")).lower() == str(account_code).lower():
                try:
                    total += Decimal(str(item.get("LineAmount", 0)))
                except (ValueError, TypeError, InvalidOperation):
                    pass
    return total


def sum_invoices(invoices: list[dict[str, Any]]) -> Decimal:
    """Sum total of all invoices."""
    return sum_field(invoices, "Total")


def filter_by_field(
    records: list[dict[str, Any]], field: str, value: str
) -> list[dict[str, Any]]:
    """Filter records where field matches value (case-insensitive)."""
    result = []
    for record in records:
        record_value = get_nested_field(record, field)
        if record_value is not None and str(record_value).lower() == str(value).lower():
            result.append(record)
    return result


def filter_by_status(
    records: list[dict[str, Any]], status: str
) -> list[dict[str, Any]]:
    """Filter records by Status field."""
    return filter_by_field(records, "Status", status)


def filter_by_contact(
    records: list[dict[str, Any]],
    contact_id: str | None = None,
    contact_name: str | None = None,
) -> list[dict[str, Any]]:
    """Filter records by Contact.ContactID or Contact.Name."""
    result = []
    for record in records:
        if contact_id:
            record_contact_id = get_nested_field(record, "Contact.ContactID")
            if (
                record_contact_id
                and str(record_contact_id).lower() == str(contact_id).lower()
            ):
                result.append(record)
        elif contact_name:
            record_contact_name = get_nested_field(record, "Contact.Name")
            if (
                record_contact_name
                and str(record_contact_name).lower() == str(contact_name).lower()
            ):
                result.append(record)
    return result


def parse_date(date_str: str | None) -> datetime | None:
    """Parse various date formats from Xero data.

    Always returns an offset-aware datetime in UTC to ensure consistent comparisons.

    Supported formats:
    - Xero's /Date(timestamp)/ format
    - ISO date: YYYY-MM-DD
    - ISO datetime: YYYY-MM-DDTHH:MM:SS
    - SQLite datetime: YYYY-MM-DD HH:MM:SS
    """
    if not date_str:
        return None

    # Handle Xero's /Date(timestamp)/ format
    if date_str.startswith("/Date("):
        try:
            # Extract timestamp from /Date(1726358400000+0000)/
            ts_str = (
                date_str.replace("/Date(", "").split("+")[0].split("-")[0].rstrip(")/")
            )
            timestamp = int(ts_str) / 1000  # Convert milliseconds to seconds
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (ValueError, TypeError, InvalidOperation):
            pass

    # Try SQLite datetime format (YYYY-MM-DD HH:MM:SS)
    try:
        if " " in date_str and "T" not in date_str:
            naive_dt = datetime.strptime(date_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
            return naive_dt.replace(tzinfo=UTC)
    except ValueError:
        pass

    # Try common date formats
    try:
        # Handle ISO format with optional time component
        date_part = date_str.split("T")[0] if "T" in date_str else date_str
        naive_dt = datetime.strptime(date_part, "%Y-%m-%d")
        return naive_dt.replace(tzinfo=UTC)
    except ValueError:
        pass

    return None


def get_days_past_due(due_date_str: str | None, reference_date: datetime) -> int:
    """Calculate days past due from due date to reference date."""
    due_date = parse_date(due_date_str)
    if not due_date:
        return 0

    delta = reference_date - due_date
    return max(0, delta.days)


def get_aging_bucket(days_past_due: int) -> str:
    """Determine aging bucket based on days past due."""
    if days_past_due <= 30:
        return "0-30"
    elif days_past_due <= 60:
        return "31-60"
    elif days_past_due <= 90:
        return "61-90"
    else:
        return "90+"


def filter_invoices_by_aging_bucket(
    invoices: list[dict[str, Any]], bucket: str, reference_date: datetime
) -> list[dict[str, Any]]:
    """Filter invoices by aging bucket."""
    result = []
    for invoice in invoices:
        due_date_str = invoice.get("DueDate") or invoice.get("DueDateString")
        days = get_days_past_due(due_date_str, reference_date)
        if get_aging_bucket(days) == bucket:
            result.append(invoice)
    return result


def filter_overdue_invoices(
    invoices: list[dict[str, Any]], reference_date: datetime, status: str = "AUTHORISED"
) -> list[dict[str, Any]]:
    """Filter invoices that are overdue (past due date and still AUTHORISED)."""
    result = []
    for invoice in invoices:
        if invoice.get("Status", "").upper() != status.upper():
            continue
        due_date_str = invoice.get("DueDate") or invoice.get("DueDateString")
        due_date = parse_date(due_date_str)
        if due_date and due_date < reference_date:
            result.append(invoice)
    return result


def sum_invoices_by_contact(
    invoices: list[dict[str, Any]],
    contact_id: str | None = None,
    contact_name: str | None = None,
) -> Decimal:
    """Sum invoice totals for a specific contact."""
    filtered = filter_by_contact(invoices, contact_id, contact_name)
    return sum_field(filtered, "Total")


def sum_amount_due(invoices: list[dict[str, Any]]) -> Decimal:
    """Sum AmountDue field across all invoices."""
    return sum_field(invoices, "AmountDue")


def calculate_cash_balance_with_starting(
    transactions: list[dict[str, Any]], starting_balance: Decimal
) -> Decimal:
    """Calculate cash balance: starting + RECEIVE - SPEND (includes all statuses)."""
    balance = starting_balance
    for txn in transactions:
        tx_type = txn.get("Type", "").upper()
        try:
            total = Decimal(str(txn.get("Total", 0)))
            if tx_type == "RECEIVE":
                balance += total
            elif tx_type == "SPEND":
                balance -= total
        except (ValueError, TypeError, InvalidOperation):
            pass
    return balance


def get_account_balance(
    accounts: list[dict[str, Any]],
    code: str | None = None,
    account_id: str | None = None,
) -> Decimal | None:
    """Get AccountBalance field for an account by code or ID."""
    for account in accounts:
        if code and str(account.get("Code", "")).lower() == str(code).lower():
            balance = account.get("AccountBalance")
            if balance is not None:
                try:
                    return Decimal(str(balance))
                except (ValueError, TypeError, InvalidOperation):
                    pass
        if (
            account_id
            and str(account.get("AccountID", "")).lower() == str(account_id).lower()
        ):
            balance = account.get("AccountBalance")
            if balance is not None:
                try:
                    return Decimal(str(balance))
                except (ValueError, TypeError, InvalidOperation):
                    pass
    return None


def calculate_account_balance(
    accounts: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    bank_transactions: list[dict[str, Any]],
    payments: list[dict[str, Any]],
    code: str | None = None,
    account_id: str | None = None,
) -> Decimal | None:
    """
    Calculate account balance from transaction data for a SINGLE account lookup.

    The mercor-xero MCP server does not store account balances, so we calculate
    them from transaction data based on account type:
    - BANK accounts: Sum of ALL bank transactions (RECEIVE = +, SPEND = -)
    - CURRENT/RECEIVABLE accounts (AR): Sum of ALL ACCREC invoice AmountDue
    - LIABILITY/PAYABLE accounts (AP): Sum of ALL ACCPAY invoice AmountDue (negative)

    IMPORTANT: This function returns the TOTAL balance for all transactions of the
    account's type, not filtered to a specific account. This works correctly when:
    - Looking up a single account's balance (e.g., for "Account Balance" field check)
    - There's only one account per type (typical Xero setup)

    DO NOT use this in a loop over multiple accounts of the same type (e.g., in
    ratio calculations) as it will cause double-counting. For ratio calculations,
    compute totals directly from transactions once.

    Args:
        accounts: List of account dicts from xero_state
        invoices: List of invoice dicts from xero_state
        bank_transactions: List of bank transaction dicts from xero_state
        payments: List of payment dicts from xero_state (unused, for future extension)
        code: Account code to look up
        account_id: Account ID to look up

    Returns:
        Calculated account balance as Decimal, or None if account not found
    """
    account = None
    for acc in accounts:
        if code and str(acc.get("Code", "")).lower() == str(code).lower():
            account = acc
            break
        if (
            account_id
            and str(acc.get("AccountID", "")).lower() == str(account_id).lower()
        ):
            account = acc
            break

    if not account:
        return None

    acc_type = (account.get("Type") or "").upper()
    acc_class = (account.get("Class") or "").upper()

    # BANK accounts: sum bank transactions
    if acc_type == "BANK":
        balance = Decimal("0")
        for txn in bank_transactions:
            tx_type = (txn.get("Type") or "").upper()
            try:
                total = Decimal(str(txn.get("Total", 0)))
                if tx_type == "RECEIVE":
                    balance += total
                elif tx_type == "SPEND":
                    balance -= total
            except (ValueError, TypeError, InvalidOperation):
                pass
        return balance

    # AR accounts (CURRENT type with CURASSET class, or RECEIVABLE): sum ACCREC invoice AmountDue
    if (
        acc_type in ("CURRENT", "RECEIVABLE")
        or acc_class == "CURASSET"
        or "RECEIVABLE" in acc_class
    ):
        balance = Decimal("0")
        for inv in invoices:
            if inv.get("Type") == "ACCREC":
                amount_due = inv.get("AmountDue")
                if amount_due is None:
                    amount_due = inv.get("Total", 0)
                try:
                    balance += Decimal(str(amount_due))
                except (ValueError, TypeError, InvalidOperation):
                    pass
        return balance

    # AP accounts (LIABILITY class or PAYABLE type): sum ACCPAY invoice AmountDue (as negative)
    if (
        acc_class in ("LIABILITY", "LIAB")
        or "PAYABLE" in acc_type
        or "LIAB" in acc_type
    ):
        balance = Decimal("0")
        for inv in invoices:
            if inv.get("Type") == "ACCPAY":
                amount_due = inv.get("AmountDue")
                if amount_due is None:
                    amount_due = inv.get("Total", 0)
                try:
                    balance -= Decimal(str(amount_due))  # Negative for liability
                except (ValueError, TypeError, InvalidOperation):
                    pass
        return balance

    return Decimal("0")
