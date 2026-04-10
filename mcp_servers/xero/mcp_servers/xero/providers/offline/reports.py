"""Reports resource implementation for offline provider - database-backed."""

import json
from calendar import monthrange
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select

from mcp_servers.xero.db.models.account import Account
from mcp_servers.xero.db.models.bank_transaction import BankTransaction
from mcp_servers.xero.db.models.invoice import Invoice, normalize_xero_date
from mcp_servers.xero.db.models.journal import Journal
from mcp_servers.xero.db.models.payment import Payment
from mcp_servers.xero.db.session import async_session


async def _calculate_account_balances_from_journals(
    report_date: datetime | None = None,
) -> dict[str, float]:
    """
    Calculate account balances from journal entries.

    This is the most accurate method for calculating balances as journals
    represent the double-entry bookkeeping entries in Xero.

    For each JournalLine:
    - NetAmount positive = Debit
    - NetAmount negative = Credit

    Account balance calculation (accounting conventions):
    - ASSET accounts: Balance = Sum of NetAmounts (Debits - Credits)
    - LIABILITY accounts: Balance = -Sum of NetAmounts (Credits - Debits)
    - EQUITY accounts: Balance = -Sum of NetAmounts (Credits - Debits)
    - REVENUE accounts: Balance = -Sum of NetAmounts (Credits - Debits)
    - EXPENSE accounts: Balance = Sum of NetAmounts (Debits - Credits)

    Args:
        report_date: Optional date to filter journals up to (inclusive).
                    If None, includes all journals.

    Returns:
        Dictionary mapping account_id to calculated balance
    """
    async with async_session() as session:
        result = await session.execute(select(Journal))
        journals = result.scalars().all()

    # Accumulate net amounts by account
    account_net_amounts: dict[str, float] = {}  # account_id -> sum of NetAmounts

    for journal in journals:
        # Filter by date if specified
        if report_date:
            # Skip journals without dates when date filtering is active
            if not journal.journal_date:
                continue
            try:
                journal_date = datetime.strptime(journal.journal_date, "%Y-%m-%d")  # type: ignore
                if journal_date > report_date:
                    continue
            except ValueError:
                # Skip journals with unparseable dates when filtering
                continue

        # Parse journal lines
        try:
            journal_lines = json.loads(journal.journal_lines) if journal.journal_lines else []
            if not isinstance(journal_lines, list):
                journal_lines = []
        except (json.JSONDecodeError, TypeError):
            continue

        for line in journal_lines:
            if not isinstance(line, dict):
                continue
            account_id = line.get("AccountID")
            if not account_id:
                continue

            try:
                net_amount = float(line.get("NetAmount", 0) or 0)
            except (ValueError, TypeError):
                continue

            if account_id not in account_net_amounts:
                account_net_amounts[account_id] = 0.0
            account_net_amounts[account_id] += net_amount

    return account_net_amounts


async def _calculate_balances_from_journals_for_balance_sheet(
    accounts: list[Account],
    journals: list,
    report_date: datetime,
) -> dict[str, float]:
    """
    Calculate account balances from journal entries for balance sheet reporting.

    This function provides the most accurate balance sheet calculation by:
    1. Starting with opening balances from account records
    2. Adding all journal entry impacts up to the report date

    Journal entries are the source of truth in Xero - all transactions
    (invoices, payments, bank transactions) ultimately create journal entries.
    Using journals directly captures:
    - Auto-generated entries from invoices/payments
    - Manual journal entries (depreciation, accruals, adjustments)
    - Any corrections or adjustments

    For each JournalLine:
    - NetAmount positive = Debit to account
    - NetAmount negative = Credit to account

    Args:
        accounts: List of Account objects (for opening balances)
        journals: List of Journal objects
        report_date: Date to calculate balances as of (inclusive)

    Returns:
        Dictionary mapping account_id to calculated balance
    """
    balances: dict[str, float] = {}

    # Step 1: Apply opening balances from accounts
    for account in accounts:
        if not account.account_id:
            continue
        opening_balance = account.opening_balance or 0.0
        try:
            balances[account.account_id] = float(opening_balance)
        except (ValueError, TypeError):
            balances[account.account_id] = 0.0

    # Step 2: Apply journal entries up to report date
    for journal in journals:
        # Filter by date - only include journals on or before report date
        if not journal.journal_date:
            continue
        try:
            journal_date = datetime.strptime(journal.journal_date, "%Y-%m-%d")
            if journal_date > report_date:
                continue
        except ValueError:
            # Skip journals with unparseable dates
            continue

        # Parse journal lines
        try:
            journal_lines = json.loads(journal.journal_lines) if journal.journal_lines else []
            if not isinstance(journal_lines, list):
                journal_lines = []
        except (json.JSONDecodeError, TypeError):
            continue

        # Apply each journal line to account balances
        for line in journal_lines:
            if not isinstance(line, dict):
                continue
            account_id = line.get("AccountID")
            if not account_id:
                continue

            try:
                net_amount = float(line.get("NetAmount", 0) or 0)
            except (ValueError, TypeError):
                continue

            if net_amount == 0:
                continue

            # Initialize account if not seen before
            if account_id not in balances:
                balances[account_id] = 0.0

            # Add the net amount to the account balance
            # In double-entry bookkeeping:
            # - Positive NetAmount = Debit (increases assets/expenses)
            # - Negative NetAmount = Credit (increases liabilities/equity/revenue)
            balances[account_id] += net_amount

    return balances


async def _calculate_bank_balances_from_transactions(
    report_date: datetime | None = None,
) -> dict[str, float]:
    """
    Calculate bank account balances from bank transactions.

    This is a fallback method when journals don't provide bank account balances.
    Uses BankTransaction records to sum up RECEIVE (adds to balance) and
    SPEND (subtracts from balance) transactions.

    Note: BankTransaction model doesn't store bank_account_id directly,
    so we parse it from line_items where bank account info may be stored,
    or use the total to update a general bank balance.

    Args:
        report_date: Optional date to filter transactions up to (inclusive).
                    If None, includes all transactions.

    Returns:
        Dictionary mapping account_code to calculated balance
    """
    async with async_session() as session:
        result = await session.execute(select(BankTransaction))
        transactions = result.scalars().all()

    # Bank transactions affect bank account balances:
    # - RECEIVE: increases bank balance (money coming in)
    # - SPEND: decreases bank balance (money going out)
    # We'll track by line item account codes since BankTransaction doesn't store bank_account_id

    bank_balances: dict[str, float] = {}

    for txn in transactions:
        # Only include AUTHORISED transactions
        if txn.status != "AUTHORISED":
            continue

        # Filter by date if specified
        if report_date:
            # Skip transactions without dates when date filtering is active
            if not txn.date:
                continue
            try:
                txn_date_str = txn.date
                if "T" in txn_date_str:
                    txn_date_str = txn_date_str.split("T")[0]
                txn_date = datetime.strptime(txn_date_str, "%Y-%m-%d")
                if txn_date > report_date:
                    continue
            except ValueError:
                # Skip transactions with unparseable dates when filtering
                continue

        # Get transaction total
        total = txn.total or 0.0

        # Bank transactions affect bank accounts based on type:
        # For tracking purposes, we'll use "BANK" as a general key
        # In reality, we'd need bank_account_id from the transaction
        if txn.type == "RECEIVE":
            bank_balances["BANK"] = bank_balances.get("BANK", 0.0) + total
        elif txn.type == "SPEND":
            bank_balances["BANK"] = bank_balances.get("BANK", 0.0) - total

    return bank_balances


# =============================================================================
# SHARED CALCULATION FUNCTIONS
# These functions provide consistent AR, AP, and Cash calculations across all
# reports (Balance Sheet, Aged AR/AP, Executive Summary) to ensure reconciliation.
# =============================================================================


def _calculate_total_ar(
    invoices: list[Invoice],
    as_of_date: datetime | None = None,
) -> float:
    """
    Calculate total Accounts Receivable balance.

    AR = Sum of amount_due for all ACCREC invoices that are AUTHORISED or SUBMITTED.
    This matches the Aged AR report calculation for consistency.

    Args:
        invoices: List of Invoice objects
        as_of_date: Optional date filter (include invoices dated on or before)

    Returns:
        Total AR balance (positive value)
    """
    total = 0.0
    for invoice in invoices:
        # Only ACCREC (customer invoices) contribute to AR
        if invoice.type != "ACCREC":
            continue

        # Only include finalized invoices (not DRAFT, VOIDED, DELETED)
        if invoice.status not in ("AUTHORISED", "SUBMITTED", "PAID"):
            continue

        # Date filter if specified
        if as_of_date and invoice.date:
            try:
                invoice_date = datetime.strptime(normalize_xero_date(invoice.date), "%Y-%m-%d")
                if invoice_date > as_of_date:
                    continue
            except ValueError:
                pass

        # Use amount_due (already accounts for payments and credit notes)
        amount_due = invoice.amount_due or 0.0
        if amount_due > 0:
            total += amount_due

    return total


def _calculate_total_ap(
    invoices: list[Invoice],
    as_of_date: datetime | None = None,
) -> float:
    """
    Calculate total Accounts Payable balance.

    AP = Sum of amount_due for all ACCPAY invoices that are AUTHORISED or SUBMITTED.
    This matches the Aged AP report calculation for consistency.

    Args:
        invoices: List of Invoice objects
        as_of_date: Optional date filter (include invoices dated on or before)

    Returns:
        Total AP balance (positive value, represents liability)
    """
    total = 0.0
    for invoice in invoices:
        # Only ACCPAY (supplier bills) contribute to AP
        if invoice.type != "ACCPAY":
            continue

        # Only include finalized invoices (not DRAFT, VOIDED, DELETED)
        if invoice.status not in ("AUTHORISED", "SUBMITTED", "PAID"):
            continue

        # Date filter if specified
        if as_of_date and invoice.date:
            try:
                invoice_date = datetime.strptime(normalize_xero_date(invoice.date), "%Y-%m-%d")
                if invoice_date > as_of_date:
                    continue
            except ValueError:
                pass

        # Use amount_due (already accounts for payments and credit notes)
        amount_due = invoice.amount_due or 0.0
        if amount_due > 0:
            total += amount_due

    return total


def _calculate_cash_balance(
    accounts: list[Account],
    bank_transactions: list[BankTransaction],
    as_of_date: datetime | None = None,
) -> float:
    """
    Calculate total Cash/Bank balance.

    Cash = Opening balances from bank accounts + All bank transaction impacts.
    Includes ALL authorized bank transactions, not just those linked to invoices.

    Args:
        accounts: List of Account objects (to get opening balances)
        bank_transactions: List of BankTransaction objects
        as_of_date: Optional date filter

    Returns:
        Total cash balance
    """
    total = 0.0

    # 1. Add opening balances from bank accounts
    for account in accounts:
        account_type = (account.type or "").upper()
        if "BANK" in account_type:
            opening_balance = account.opening_balance or 0.0
            try:
                total += float(opening_balance)
            except (ValueError, TypeError):
                pass

    # 2. Add ALL authorized bank transactions (not just linked ones)
    for txn in bank_transactions:
        # Only include authorized transactions
        if txn.status != "AUTHORISED":
            continue

        # Date filter if specified
        if as_of_date and txn.date:
            try:
                txn_date_str = txn.date
                if "T" in txn_date_str:
                    txn_date_str = txn_date_str.split("T")[0]
                txn_date = datetime.strptime(txn_date_str, "%Y-%m-%d")
                if txn_date > as_of_date:
                    continue
            except ValueError:
                pass

        # Get transaction total
        txn_total = txn.total or 0.0
        try:
            txn_total = float(txn_total)
        except (ValueError, TypeError):
            continue

        # RECEIVE increases cash, SPEND decreases cash
        if txn.type == "RECEIVE":
            total += txn_total
        elif txn.type == "SPEND":
            total -= txn_total

    return total


async def _calculate_pnl_from_journals(
    from_date: datetime,
    to_date: datetime,
    accounts: list[Account],
) -> dict[str, Any]:
    """
    Calculate P&L amounts from journal entries within a date range.

    This is the most accurate method for P&L as it captures ALL postings including:
    - Invoice/bill postings
    - Manual journal entries (depreciation, accruals, adjustments)
    - Bank transaction postings

    For each JournalLine:
    - Revenue accounts: Income = -NetAmount (credits are negative in Xero)
    - Expense accounts: Expense = +NetAmount (debits are positive in Xero)

    Args:
        from_date: Start of P&L period (inclusive)
        to_date: End of P&L period (inclusive)
        accounts: List of Account objects for type classification

    Returns:
        Dictionary with:
        - income_by_account: {account_id: amount}
        - expense_by_account: {account_id: amount}
        - total_income: float
        - total_expenses: float
        - has_journal_data: bool (False if no journals found)
    """
    # Build account lookup by account_id
    account_info: dict[str, dict[str, str]] = {}
    for account in accounts:
        account_info[account.account_id] = {
            "type": (account.type or "").upper(),
            "class": (account.class_ or "").upper(),
            "name": account.name or "",
            "code": account.code or "",
        }

    def is_revenue_account(account_id: str) -> bool:
        """Check if account is a revenue account by account_id."""
        info = account_info.get(account_id, {})
        account_type = info.get("type", "")
        account_class = info.get("class", "")
        return (
            "REVENUE" in account_type
            or "REVENUE" in account_class
            or "INCOME" in account_type
            or "SALES" in account_type
        )

    def is_expense_account(account_id: str) -> bool:
        """Check if account is an expense account by account_id."""
        info = account_info.get(account_id, {})
        account_type = info.get("type", "")
        account_class = info.get("class", "")
        # Note: DEPRECIATN is a contra-asset account type (e.g., Accumulated Depreciation)
        # and belongs on the balance sheet, NOT in P&L. The depreciation expense
        # would be a separate account with type EXPENSE.
        return (
            "EXPENSE" in account_type
            or "EXPENSE" in account_class
            or "EXP" in account_type
            or "DIRECTCOSTS" in account_type
            or "OVERHEADS" in account_type
        )

    # Query journals
    async with async_session() as session:
        result = await session.execute(select(Journal))
        journals = result.scalars().all()

    income_by_account: dict[str, float] = {}
    expense_by_account: dict[str, float] = {}
    total_income = 0.0
    total_expenses = 0.0
    journal_count = 0

    for journal in journals:
        # Filter by date range
        if not journal.journal_date:
            continue
        try:
            journal_date = datetime.strptime(journal.journal_date, "%Y-%m-%d")
            if not (from_date <= journal_date <= to_date):
                continue
        except ValueError:
            continue

        # Parse journal lines
        try:
            journal_lines = json.loads(journal.journal_lines) if journal.journal_lines else []
            if not isinstance(journal_lines, list):
                journal_lines = []
        except (json.JSONDecodeError, TypeError):
            continue

        # Only count journal after successful parsing with actual lines
        if not journal_lines:
            continue
        journal_count += 1

        for line in journal_lines:
            if not isinstance(line, dict):
                continue
            account_id = line.get("AccountID")
            if not account_id:
                continue

            try:
                net_amount = float(line.get("NetAmount", 0) or 0)
            except (ValueError, TypeError):
                continue

            if net_amount == 0:
                continue

            # Revenue accounts: credits (negative NetAmount) = income
            # Allow negative income_amount to handle reversals (refunds, adjustments)
            if is_revenue_account(account_id):
                # Revenue is credited (negative in Xero), so negate to get positive income
                # Reversals (debits to revenue) will result in negative income_amount
                income_amount = -net_amount
                income_by_account[account_id] = (
                    income_by_account.get(account_id, 0.0) + income_amount
                )
                total_income += income_amount

            # Expense accounts: debits (positive NetAmount) = expense
            # Allow negative amounts to handle reversals (accrual reversals, corrections)
            elif is_expense_account(account_id):
                # Expenses are debited (positive in Xero)
                # Reversals (credits to expense) will have negative net_amount
                expense_by_account[account_id] = (
                    expense_by_account.get(account_id, 0.0) + net_amount
                )
                total_expenses += net_amount

    return {
        "income_by_account": income_by_account,
        "expense_by_account": expense_by_account,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "has_journal_data": journal_count > 0,
    }


def _calculate_tax_position(
    invoices: list[Invoice],
    as_of_date: datetime | None = None,
) -> dict[str, float]:
    """
    Calculate tax control account balances.

    This calculates the GST/VAT position based on invoice tax amounts:
    - Tax collected: TotalTax from ACCREC (sales) invoices
    - Tax paid: TotalTax from ACCPAY (purchase) invoices
    - Net tax liability: collected - paid (positive = owe tax, negative = refund due)

    Args:
        invoices: List of Invoice objects
        as_of_date: Optional date filter (include invoices dated on or before)

    Returns:
        Dictionary with tax_collected, tax_paid, net_tax_liability
    """
    tax_collected = 0.0
    tax_paid = 0.0

    for invoice in invoices:
        # Only include finalized invoices
        if invoice.status not in ("AUTHORISED", "SUBMITTED", "PAID"):
            continue

        # Date filter if specified
        if as_of_date and invoice.date:
            try:
                invoice_date = datetime.strptime(normalize_xero_date(invoice.date), "%Y-%m-%d")
                if invoice_date > as_of_date:
                    continue
            except ValueError:
                pass

        # Get tax amount
        total_tax = invoice.total_tax or 0.0
        try:
            total_tax = float(total_tax)
        except (ValueError, TypeError):
            total_tax = 0.0

        if total_tax <= 0:
            continue

        # ACCREC = sales invoices, tax collected from customers
        if invoice.type == "ACCREC":
            tax_collected += total_tax
        # ACCPAY = purchase invoices, tax paid to suppliers
        elif invoice.type == "ACCPAY":
            tax_paid += total_tax

    return {
        "tax_collected": tax_collected,
        "tax_paid": tax_paid,
        "net_tax_liability": tax_collected - tax_paid,
    }


def _categorize_account(account: Account) -> tuple[str | None, str | None]:
    """Determine the balance sheet section/subsection for an account."""
    name = (account.name or "").lower()
    account_type = (account.type or "").upper()
    account_class = (account.class_ or "").upper()
    is_asset_account = "ASSET" in account_class or "ASSET" in account_type
    long_term_marker = "long-term" in name or "long term" in name
    short_term_marker = "short-term" in name or "short term" in name

    if "EQUITY" in account_class or "EQUITY" in account_type:
        return "Equity", None

    if "LIAB" in account_class or "LIAB" in account_type or "liability" in name:
        if short_term_marker:
            subsection = "Current Liabilities"
        elif long_term_marker:
            subsection = "Long-term Liabilities"
        else:
            subsection = "Current Liabilities"
        return "Liabilities", subsection
    if not is_asset_account and ("payable" in name or "loan" in name or "accrued" in name):
        if short_term_marker:
            subsection = "Current Liabilities"
        elif long_term_marker:
            subsection = "Long-term Liabilities"
        else:
            subsection = "Current Liabilities"
        return "Liabilities", subsection

    if not account_class and not account_type and "stock" in name:
        return "Equity", None

    if (
        "ASSET" in account_class
        or "ASSET" in account_type
        or "BANK" in account_type
        or "CURRENT" in account_type
    ):
        subsection = "Current Assets"
        if (
            "FIXED" in account_type
            or "equipment" in name
            or "furniture" in name
            or "depreciation" in name
            or "long-term" in name
        ):
            subsection = "Fixed Assets"
        return "Assets", subsection

    if "asset" in name:
        return "Assets", "Current Assets"

    return None, None


def _value_for_display(balance: float, section: str) -> float:
    """Normalize sign for display based on section."""
    if section in {"Liabilities", "Equity"}:
        return abs(balance)
    return balance


def _value_cells(
    values: list[float], attributes: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Create value cells for each comparison period."""
    return [{"Value": f"{value:.2f}", "Attributes": attributes} for value in values]


def _build_account_cells(
    account: Account, values: list[float], section: str
) -> list[dict[str, object]]:
    """Build the name/value cell pair for an account row."""
    display_values = [_value_for_display(value, section) for value in values]
    attributes = []
    if account.account_id:
        attributes = [{"Value": account.account_id, "Id": "account"}]

    return [
        {"Value": account.name or account.code or "Account", "Attributes": attributes},
        *_value_cells(display_values, attributes),
    ]


def _build_account_row(account: Account, values: list[float], section: str) -> dict[str, object]:
    """Construct a row entry for a single account."""
    return {
        "RowType": "Row",
        "Cells": _build_account_cells(account, values, section),
    }


def _build_summary_row(label: str, values: list[float]) -> dict[str, object]:
    """Construct a summary row for totals."""
    return {
        "RowType": "SummaryRow",
        "Cells": [{"Value": label}, *_value_cells(values, [])],
    }


def _collect_account_sections(
    accounts: list[Account], account_value_series: dict[str, list[float]]
) -> dict[str, dict[str, list[tuple[Account, list[float]]]]]:
    """Group accounts into balance sheet sections/subsections."""
    sections = {
        "Assets": {"Current Assets": [], "Fixed Assets": []},
        "Liabilities": {"Current Liabilities": [], "Long-term Liabilities": []},
        "Equity": [],
    }

    for account in accounts:
        account_id = account.account_id
        if not account_id:
            continue

        values = account_value_series.get(account_id)
        if not values:
            continue

        if max(abs(value) for value in values) < 0.005:
            continue

        section, subsection = _categorize_account(account)

        if section == "Assets" and subsection:
            sections["Assets"].setdefault(subsection, []).append((account, values))
        elif section == "Liabilities" and subsection:
            sections["Liabilities"].setdefault(subsection, []).append((account, values))
        elif section == "Equity":
            sections["Equity"].append((account, values))

    return sections


def _build_section_from_subsections(
    title: str,
    subsection_keys: list[str],
    entries: dict[str, list[tuple[Account, list[float]]]],
    section_name: str,
    period_count: int,
) -> tuple[dict[str, object], list[float]]:
    """Build a main section node from its subsections."""
    rows: list[dict[str, object]] = []
    total_values = [0.0] * period_count

    for subsection in subsection_keys:
        subsection_accounts = entries.get(subsection, [])
        if not subsection_accounts:
            continue

        subsection_rows = [
            _build_account_row(account, values, section_name)
            for account, values in subsection_accounts
        ]
        subsection_total = _sum_period_values(subsection_accounts, section_name, period_count)
        subsection_rows.append(_build_summary_row(f"Total {subsection}", subsection_total))

        rows.append({"RowType": "Section", "Title": subsection, "Rows": subsection_rows})
        total_values = [
            subtotal + current
            for subtotal, current in zip(total_values, subsection_total, strict=True)
        ]

    rows.append(_build_summary_row(f"Total {title}", total_values))

    return {"RowType": "Section", "Title": title, "Rows": rows}, total_values


def _build_equity_section(
    entries: list[tuple[Account, list[float]]],
    period_count: int,
    net_income: list[float] | None = None,
) -> tuple[dict[str, object], list[float]]:
    """Build the equity section rows.

    Args:
        entries: List of equity accounts with their period values
        period_count: Number of periods in the report
        net_income: Optional net income values for each period to add as "Current Year Earnings"
    """
    rows: list[dict[str, object]] = []
    total_values = [0.0] * period_count

    for account, values in entries:
        rows.append(_build_account_row(account, values, "Equity"))
        for idx, value in enumerate(values):
            total_values[idx] += _value_for_display(value, "Equity")

    # Add Current Year Earnings (net income) to balance the accounting equation
    # This represents the P&L impact that flows into equity
    # Only show if there's meaningful P&L activity (threshold of 0.01)
    if net_income and any(abs(v) >= 0.01 for v in net_income):
        earnings_row = {
            "RowType": "Row",
            "Cells": [{"Value": "Current Year Earnings"}]
            + [{"Value": f"{value:.2f}"} for value in net_income],
        }
        rows.append(earnings_row)
        for idx, value in enumerate(net_income):
            total_values[idx] += value

    rows.append(_build_summary_row("Total Equity", total_values))

    return {"RowType": "Section", "Title": "Equity", "Rows": rows}, total_values


def _shift_date_by_months(date: datetime, months: int) -> datetime:
    """Shift a date by a number of months while preserving day bounds."""
    month = date.month - 1 + months
    year = date.year + month // 12
    month = month % 12 + 1
    day = min(date.day, monthrange(year, month)[1])
    return date.replace(year=year, month=month, day=day)


def _get_timeframe_delta_months(timeframe: str | None) -> int:
    timeframe_upper = (timeframe or "MONTH").upper()
    return {
        "YEAR": 12,
        "QUARTER": 3,
    }.get(timeframe_upper, 1)


def _get_period_dates(report_date: datetime, count: int, timeframe: str | None) -> list[datetime]:
    """Return the sequence of report dates for each comparison period."""
    delta_months = _get_timeframe_delta_months(timeframe)
    return [
        _shift_date_by_months(report_date, -offset * delta_months)
        for offset in reversed(range(count))
    ]


def _generate_period_labels(period_dates: list[datetime]) -> list[str]:
    """Generate display labels for each period."""
    return [date.strftime("%d %b %Y") for date in period_dates]


def _build_balance_sheet_header(period_labels: list[str]) -> dict[str, object]:
    """Construct the header row reflecting comparison periods."""
    cells = [{"Value": ""}] + [{"Value": label} for label in period_labels]
    return {"RowType": "Header", "Cells": cells}


def _sum_period_values(
    entries: list[tuple[Account, list[float]]], section: str, period_count: int
) -> list[float]:
    """Sum values for each period while normalizing sign per section."""
    totals = [0.0] * period_count
    for _, values in entries:
        for idx, value in enumerate(values):
            totals[idx] += _value_for_display(value, section)
    return totals


def _parse_json_field(raw_value: str | list | None) -> list[dict]:
    """Safely parse JSON strings or return lists as-is."""
    if not raw_value:
        return []
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw_value, list):
        return raw_value
    return []


def _parse_amount(value: float | str | None) -> float:
    """Normalize numeric inputs to floats."""
    if value is None or value == "":
        return 0.0

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_date_to_datetime(raw_date: str | None) -> datetime | None:
    normalized = normalize_xero_date(raw_date)
    if not normalized:
        return None

    try:
        return datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError:
        return None


def _normalize_code(code: str | None) -> str:
    if not code:
        return ""
    return str(code).strip().upper()


def _find_account_id(
    accounts: list[Account],
    *,
    codes: list[str] | None = None,
    name_contains: list[str] | None = None,
    type_contains: list[str] | None = None,
    class_equals: str | None = None,
) -> str | None:
    codes = [c.strip().upper() for c in (codes or []) if c]
    name_contains = [keyword.strip().upper() for keyword in (name_contains or []) if keyword]
    type_contains = [keyword.strip().upper() for keyword in (type_contains or []) if keyword]
    class_equals_upper = class_equals.strip().upper() if class_equals else None
    has_filter = bool(codes or name_contains or type_contains)

    def matches(account: Account) -> bool:
        if not account.account_id:
            return False

        if class_equals_upper and account.class_:
            if account.class_.strip().upper() != class_equals_upper:
                return False
        elif class_equals_upper and not account.class_:
            return False

        code_match = True
        if codes:
            code_match = bool(account.code and account.code.strip().upper() in codes)

        name_match = True
        if name_contains:
            name_match = bool(
                account.name
                and any(keyword in account.name.strip().upper() for keyword in name_contains)
            )

        type_match = True
        if type_contains:
            type_upper = account.type.strip().upper() if account.type else ""
            type_match = any(keyword in type_upper for keyword in type_contains)

        if not has_filter:
            return True

        return code_match and name_match and type_match

    for account in accounts:
        if matches(account):
            return account.account_id

    return None


def _resolve_line_account_id(line_item: dict, accounts_by_code: dict[str, str]) -> str | None:
    account_id = line_item.get("AccountID") or line_item.get("account_id")
    if account_id:
        return account_id

    account_code = line_item.get("AccountCode") or line_item.get("account_code")
    if account_code:
        return accounts_by_code.get(_normalize_code(account_code))

    return None


def _accumulate_balance(balances: dict[str, float], account_id: str | None, amount: float) -> None:
    if not account_id or amount == 0:
        return

    balances[account_id] = balances.get(account_id, 0.0) + amount


def _apply_opening_balances(balances: dict[str, float], accounts: list[Account]) -> None:
    for account in accounts:
        if not account.account_id:
            continue
        opening_balance = account.opening_balance or 0.0
        if opening_balance:
            _accumulate_balance(balances, account.account_id, float(opening_balance))


def _apply_invoice_balances(
    balances: dict[str, float],
    invoice: Invoice,
    accounts_by_code: dict[str, str],
    ar_account_id: str | None,
    ap_account_id: str | None,
    tax_payable_account_id: str | None,
    tax_receivable_account_id: str | None,
    report_date: datetime,
) -> None:
    if not invoice:
        return

    status = (invoice.status or "").upper()
    valid_statuses = {"AUTHORISED", "PAID", "SUBMITTED"}
    if status and status not in valid_statuses:
        return

    invoice_date = _parse_date_to_datetime(invoice.date)
    if invoice_date and invoice_date > report_date:
        return

    line_items = _parse_json_field(invoice.line_items)
    total_amount = _parse_amount(invoice.total)
    if total_amount == 0.0:
        total_amount = sum(
            _parse_amount(item.get("LineAmount")) for item in line_items if isinstance(item, dict)
        )

    if total_amount == 0.0:
        return

    invoice_type = (invoice.type or "").upper()

    if invoice_type == "ACCREC":
        if not ar_account_id:
            logger.debug("Skipping ACCREC invoice - Accounts Receivable account not found.")
            return

        _accumulate_balance(balances, ar_account_id, total_amount)

        for line in line_items:
            amount = _parse_amount(line.get("LineAmount"))
            if amount == 0.0:
                continue
            line_account_id = _resolve_line_account_id(line, accounts_by_code)
            _accumulate_balance(balances, line_account_id, -amount)

        tax_amount = _parse_amount(invoice.total_tax)
        if tax_amount:
            tax_target = tax_payable_account_id or ap_account_id
            if tax_target:
                _accumulate_balance(balances, tax_target, -tax_amount)
            else:
                logger.debug("No tax liability account found for ACCREC invoice tax.")

    elif invoice_type == "ACCPAY":
        if not ap_account_id:
            logger.debug("Skipping ACCPAY invoice - Accounts Payable account not found.")
            return

        _accumulate_balance(balances, ap_account_id, -total_amount)

        for line in line_items:
            amount = _parse_amount(line.get("LineAmount"))
            if amount == 0.0:
                continue
            line_account_id = _resolve_line_account_id(line, accounts_by_code)
            _accumulate_balance(balances, line_account_id, amount)

        tax_amount = _parse_amount(invoice.total_tax)
        if tax_amount:
            tax_target = tax_receivable_account_id or ar_account_id
            if tax_target:
                _accumulate_balance(balances, tax_target, tax_amount)
            else:
                logger.debug("No tax receivable account found for ACCPAY invoice tax.")


def _apply_payment_balances(
    balances: dict[str, float],
    payment: Payment,
    invoices_by_id: dict[str, Invoice],
    ar_account_id: str | None,
    ap_account_id: str | None,
    default_bank_account_id: str | None,
    report_date: datetime,
) -> None:
    if not payment:
        return

    status = (payment.status or "").upper()
    if status and status not in {"AUTHORISED"}:
        return

    payment_date = _parse_date_to_datetime(payment.date)
    if payment_date and payment_date > report_date:
        return

    amount = _parse_amount(payment.amount)
    if amount == 0.0:
        return

    bank_account_id = payment.account_id or default_bank_account_id
    if not bank_account_id:
        logger.debug("Skipping payment - no bank account assigned.")
        return

    invoice = invoices_by_id.get(payment.invoice_id or "")
    if not invoice:
        logger.debug(f"Skipping payment {payment.payment_id} - invoice not found.")
        return

    invoice_type = (invoice.type or "").upper()

    if invoice_type == "ACCREC":
        if not ar_account_id:
            logger.debug("Skipping ACCREC payment - Accounts Receivable account missing.")
            return
        _accumulate_balance(balances, bank_account_id, amount)
        _accumulate_balance(balances, ar_account_id, -amount)
    elif invoice_type == "ACCPAY":
        if not ap_account_id:
            logger.debug("Skipping ACCPAY payment - Accounts Payable account missing.")
            return
        _accumulate_balance(balances, bank_account_id, -amount)
        _accumulate_balance(balances, ap_account_id, amount)


def _apply_bank_transaction_balances(
    balances: dict[str, float],
    txn: BankTransaction,
    accounts_by_code: dict[str, str],
    default_bank_account_id: str | None,
    report_date: datetime,
) -> None:
    if not txn:
        return

    status = (txn.status or "").upper()
    if status and status not in {"AUTHORISED"}:
        return

    txn_date = _parse_date_to_datetime(txn.date)
    if txn_date and txn_date > report_date:
        return

    line_items = _parse_json_field(txn.line_items)
    if not line_items:
        return

    bank_account_id = txn.bank_account_id or default_bank_account_id
    if not bank_account_id:
        logger.debug("Skipping bank transaction - bank account not resolved.")
        return

    total_line_amount = sum(
        _parse_amount(item.get("LineAmount")) for item in line_items if isinstance(item, dict)
    )

    txn_type = (txn.type or "").upper()

    if txn_type == "RECEIVE":
        _accumulate_balance(balances, bank_account_id, total_line_amount)
        for line in line_items:
            amount = _parse_amount(line.get("LineAmount"))
            if amount == 0.0:
                continue
            line_account_id = _resolve_line_account_id(line, accounts_by_code)
            _accumulate_balance(balances, line_account_id, -amount)
    elif txn_type == "SPEND":
        _accumulate_balance(balances, bank_account_id, -total_line_amount)
        for line in line_items:
            amount = _parse_amount(line.get("LineAmount"))
            if amount == 0.0:
                continue
            line_account_id = _resolve_line_account_id(line, accounts_by_code)
            _accumulate_balance(balances, line_account_id, amount)


def _merge_account_activity(
    balances: dict[str, float],
    accounts: list[Account],
    invoices: list[Invoice],
    payments: list[Payment],
    bank_transactions: list[BankTransaction],
    report_date: datetime,
) -> dict[str, float]:
    merged = dict(balances)
    accounts_by_code = {
        _normalize_code(account.code): account.account_id
        for account in accounts
        if account.code and account.account_id
    }

    ar_account_id = _find_account_id(
        accounts, name_contains=["RECEIVABLE"], class_equals="ASSET", type_contains=["CURRENT"]
    )
    ap_account_id = _find_account_id(
        accounts,
        name_contains=["PAYABLE"],
        class_equals="LIABILITY",
        type_contains=["CURRENT", "CURRLIAB"],
    )
    default_bank_account_id = _find_account_id(
        accounts, type_contains=["BANK"], class_equals="ASSET"
    )
    tax_payable_account_id = _find_account_id(
        accounts,
        name_contains=["TAX", "GST", "VAT"],
        class_equals="LIABILITY",
        type_contains=["LIABILITY"],
    )
    tax_receivable_account_id = _find_account_id(
        accounts,
        name_contains=["TAX", "GST", "VAT"],
        class_equals="ASSET",
        type_contains=["CURRENT", "ASSET"],
    )

    _apply_opening_balances(merged, accounts)

    invoices_by_id = {invoice.invoice_id: invoice for invoice in invoices if invoice.invoice_id}

    for invoice in invoices:
        _apply_invoice_balances(
            merged,
            invoice,
            accounts_by_code,
            ar_account_id,
            ap_account_id,
            tax_payable_account_id,
            tax_receivable_account_id,
            report_date,
        )

    for payment in payments:
        _apply_payment_balances(
            merged,
            payment,
            invoices_by_id,
            ar_account_id,
            ap_account_id,
            default_bank_account_id,
            report_date,
        )

    for txn in bank_transactions:
        _apply_bank_transaction_balances(
            merged,
            txn,
            accounts_by_code,
            default_bank_account_id,
            report_date,
        )

    # Override AR/AP balances with direct calculation from amount_due
    # This ensures Balance Sheet AR/AP matches Aged AR/AP reports exactly
    if ar_account_id:
        ar_balance = _calculate_total_ar(invoices, report_date)
        merged[ar_account_id] = ar_balance

    if ap_account_id:
        ap_balance = _calculate_total_ap(invoices, report_date)
        # AP is a liability, stored as negative in our convention
        merged[ap_account_id] = -ap_balance

    # Override Cash/Bank balance with direct calculation including ALL bank transactions
    # This ensures unlinked bank transactions (like owner deposits) are included
    if default_bank_account_id:
        cash_balance = _calculate_cash_balance(accounts, bank_transactions, report_date)
        merged[default_bank_account_id] = cash_balance

    return merged


async def get_report_balance_sheet(
    self,
    date: str,
    periods: int | None = None,
    timeframe: str | None = None,
    tracking_categories: list[str] | None = None,
) -> dict[str, Any]:
    """
    Get balance sheet report from database.

    **Balance Calculation Strategy:**
    This function uses a two-tier approach:
    1. If journal entries exist, use journal-based calculation (most accurate)
    2. Otherwise, fall back to document-based calculation (invoices, payments, etc.)

    Journal-based calculation is preferred because:
    - Journals are the source of truth for double-entry bookkeeping
    - Manual journal entries (depreciation, accruals, adjustments) are captured
    - All transactions ultimately post to journals in Xero

    Args:
        date: Report date in YYYY-MM-DD format
        periods: Number of comparison periods to include
        timeframe: Timeframe for comparison (MONTH, QUARTER, YEAR)
        tracking_categories: Tracking category filters

    Returns:
        Balance sheet report with metadata
    """
    logger.info(f"Generating balance sheet from database for date: {date}")

    # Normalize and parse date (handles slashes, various formats)
    normalized_date = normalize_xero_date(date)
    try:
        report_date = datetime.strptime(normalized_date, "%Y-%m-%d")
        formatted_date = report_date.strftime("%d %B %Y")
    except ValueError as err:
        logger.error(f"Invalid date format: {date}")
        raise ValueError(
            f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got date='{date}'"
        ) from err

    # Query all relevant data from database
    async with async_session() as session:
        account_result = await session.execute(select(Account))
        accounts = account_result.scalars().all()

        invoice_result = await session.execute(select(Invoice))
        invoices = invoice_result.scalars().all()

        payment_result = await session.execute(select(Payment))
        payments = payment_result.scalars().all()

        bank_txn_result = await session.execute(select(BankTransaction))
        bank_transactions = bank_txn_result.scalars().all()

        journal_result = await session.execute(select(Journal))
        journals = journal_result.scalars().all()

    period_count = max(1, periods or 1)
    period_dates = _get_period_dates(report_date, period_count, timeframe)
    period_labels = _generate_period_labels(period_dates)
    header_row = _build_balance_sheet_header(period_labels)

    # Determine calculation strategy based on available data
    # Use journal-based if journals exist, otherwise fall back to document-based
    use_journal_based = len(journals) > 0

    period_balances: list[dict[str, float]] = []
    for period_date in period_dates:
        if use_journal_based:
            # Journal-based calculation: most accurate, captures all postings
            # including manual journal entries (depreciation, accruals, etc.)
            logger.debug(f"Using journal-based balance calculation for {period_date}")
            merged_balances = await _calculate_balances_from_journals_for_balance_sheet(
                accounts,
                journals,
                period_date,
            )
        else:
            # Document-based calculation: fallback when no journals available
            # Calculates from invoices, payments, bank transactions
            logger.debug(f"Using document-based balance calculation for {period_date}")
            merged_balances = _merge_account_activity(
                {},  # Start empty
                accounts,
                invoices,
                payments,
                bank_transactions,
                period_date,
            )
        period_balances.append(merged_balances)

    account_value_series: dict[str, list[float]] = {}
    for account in accounts:
        if not account.account_id:
            continue
        account_value_series[account.account_id] = [
            period_balance.get(account.account_id, 0.0) for period_balance in period_balances
        ]

    # Calculate net income (revenue - expenses) for each period
    # This is needed to balance the accounting equation: Assets = Liabilities + Equity + Net Income
    net_income = [0.0] * period_count
    for account in accounts:
        if not account.account_id:
            continue
        account_type = (account.type or "").upper()
        account_class = (account.class_ or "").upper()

        # Check if revenue account
        is_revenue = (
            "REVENUE" in account_type
            or "REVENUE" in account_class
            or "INCOME" in account_type
            or "SALES" in account_type
        )
        # Check if expense account
        # Note: DEPRECIATN is a contra-asset (balance sheet), not an expense
        is_expense = (
            "EXPENSE" in account_type
            or "EXPENSE" in account_class
            or "EXP" in account_type
            or "DIRECTCOSTS" in account_type
            or "OVERHEADS" in account_type
        )

        if is_revenue or is_expense:
            values = account_value_series.get(account.account_id, [0.0] * period_count)
            for idx, value in enumerate(values):
                if is_revenue:
                    # Revenue is stored as negative (credit balance convention)
                    # Negate to convert to positive income
                    net_income[idx] += -value
                elif is_expense:
                    # Expenses are stored as positive (debit balance convention)
                    # Subtract from net income
                    net_income[idx] -= value

    sections = _collect_account_sections(accounts, account_value_series)
    assets_section, total_assets = _build_section_from_subsections(
        "Assets",
        ["Current Assets", "Fixed Assets"],
        sections.get("Assets", {}),
        "Assets",
        period_count,
    )
    liabilities_section, total_liabilities = _build_section_from_subsections(
        "Liabilities",
        ["Current Liabilities", "Long-term Liabilities"],
        sections.get("Liabilities", {}),
        "Liabilities",
        period_count,
    )
    equity_section, total_equity = _build_equity_section(
        sections.get("Equity", []), period_count, net_income
    )

    net_assets = [
        asset - liability for asset, liability in zip(total_assets, total_liabilities, strict=True)
    ]
    rows = [
        header_row,
        assets_section,
        liabilities_section,
        equity_section,
        _build_summary_row("Net Assets", net_assets),
    ]

    report = {
        "ReportID": "BalanceSheet",
        "ReportName": "Balance Sheet",
        "ReportType": "BalanceSheet",
        "ReportTitles": ["Balance Sheet", "Demo Company (US)", f"As at {formatted_date}"],
        "ReportDate": formatted_date,
        "UpdatedDateUTC": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "Rows": rows,
    }

    if tracking_categories:
        report["TrackingCategoriesApplied"] = tracking_categories

    liabilities_plus_equity = [
        liability + equity
        for liability, equity in zip(total_liabilities, total_equity, strict=True)
    ]
    latest_assets = total_assets[-1]
    latest_liabilities = total_liabilities[-1]
    latest_equity = total_equity[-1]
    latest_liabilities_plus_equity = liabilities_plus_equity[-1]
    difference = abs(latest_assets - latest_liabilities_plus_equity)

    if difference > 0.01:
        logger.warning(
            f"Balance sheet equation violated: Assets={latest_assets:.2f}, "
            f"Liabilities={latest_liabilities:.2f}, Equity={latest_equity:.2f}, "
            f"L+E={latest_liabilities_plus_equity:.2f}, difference={difference:.2f}"
        )
    else:
        logger.debug(
            f"Balance sheet equation validated: Assets={latest_assets:.2f} = "
            f"Liabilities + Equity ({latest_liabilities_plus_equity:.2f})"
        )

    response = {"Reports": [report]}
    report_period = {"asOfDate": date}
    return self._add_metadata(response, "xero-mock", "offline", report_period=report_period)


async def get_report_profit_and_loss(
    self,
    from_date: str,
    to_date: str,
    periods: int | None = None,
    timeframe: str | None = None,
    tracking_categories: list[str] | None = None,
) -> dict[str, Any]:
    """
    Get profit and loss report from database.

    **Accounting Method:**
    This function uses journal-driven P&L when journal entries are available,
    which provides GAAP/IFRS-correct accounting including:
    - Manual adjusting entries (depreciation, accruals, deferrals)
    - Proper revenue recognition from deferred revenue accounts
    - Expense recognition from prepaid expense accounts

    If no journal entries are found, falls back to invoice-date based P&L:
    - Revenue recognized when invoice is created (not when service delivered)
    - Expenses recognized when bill is created (not when incurred)

    **For Accrual Accounting:**
    Upload journal entries (Journals CSV) with month-end adjusting entries.
    The tool will automatically use them for P&L calculation.

    Args:
        from_date: Start date in YYYY-MM-DD format
        to_date: End date in YYYY-MM-DD format
        periods: Number of comparison periods to include
        timeframe: Timeframe for comparison (MONTH, QUARTER, YEAR)
        tracking_categories: Tracking category filters

    Returns:
        Profit and loss report with metadata
    """
    logger.info(f"Generating P&L from database for period: {from_date} to {to_date}")

    # Normalize dates to YYYY-MM-DD format (handles slashes, various formats)
    normalized_from = normalize_xero_date(from_date)
    normalized_to = normalize_xero_date(to_date)

    # Parse dates
    try:
        start_date = datetime.strptime(normalized_from, "%Y-%m-%d")
        end_date = datetime.strptime(normalized_to, "%Y-%m-%d")
        formatted_date_range = (
            f"{start_date.strftime('%d %B %Y')} to {end_date.strftime('%d %B %Y')}"
        )
    except ValueError as err:
        logger.error(f"Invalid date format: {from_date} or {to_date}")
        raise ValueError(
            f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got from_date='{from_date}', to_date='{to_date}'"
        ) from err

    # Query invoices and bank transactions from database
    async with async_session() as session:
        # Get all invoices
        invoice_result = await session.execute(select(Invoice))
        invoices = invoice_result.scalars().all()

        # Get all bank transactions
        bank_txn_result = await session.execute(select(BankTransaction))
        bank_transactions = bank_txn_result.scalars().all()

        # Get accounts to determine which are revenue vs expense accounts
        account_result = await session.execute(select(Account))
        accounts = account_result.scalars().all()

    # Build account lookup for row display
    account_lookup: dict[str, dict[str, str]] = {}
    for account in accounts:
        account_lookup[account.account_id] = {
            "name": account.name or account.code or "Unknown",
            "code": account.code or "",
        }

    # Try journal-driven P&L first (most accurate - includes manual entries)
    journal_pnl = await _calculate_pnl_from_journals(start_date, end_date, accounts)

    income_rows = []
    expense_rows = []

    if journal_pnl["has_journal_data"]:
        # Use journal-driven P&L
        logger.info("Using journal-driven P&L calculation")
        total_income = journal_pnl["total_income"]
        total_expenses = journal_pnl["total_expenses"]

        # Build income rows from journal aggregation
        for account_id, amount in journal_pnl["income_by_account"].items():
            account_info = account_lookup.get(account_id, {})
            account_name = account_info.get("name", "Revenue")
            income_rows.append(
                {
                    "RowType": "Row",
                    "Cells": [
                        {"Value": account_name},
                        {"Value": f"{amount:.2f}"},
                    ],
                }
            )

        # Build expense rows from journal aggregation
        for account_id, amount in journal_pnl["expense_by_account"].items():
            account_info = account_lookup.get(account_id, {})
            account_name = account_info.get("name", "Expense")
            expense_rows.append(
                {
                    "RowType": "Row",
                    "Cells": [
                        {"Value": account_name},
                        {"Value": f"{amount:.2f}"},
                    ],
                }
            )
    else:
        # Fallback to invoice/bank transaction-based P&L
        # This is invoice-date based accounting, not accrual accounting.
        # For proper accrual P&L, upload journal entries with adjusting entries.
        if len(invoices) > 0:
            logger.warning(
                "Invoice data exists but no journal entries found. "
                "P&L will be invoice-date based, not accrual-based. "
                "For accrual accounting, upload journal entries with month-end adjustments."
            )
        else:
            logger.info("No journal data found, falling back to invoice-based P&L")

        # Build account code to type/class mapping for P&L classification
        account_info: dict[str, dict[str, str]] = {}
        for account in accounts:
            if account.code:
                account_info[account.code] = {
                    "type": (account.type or "").upper(),
                    "class": (account.class_ or "").upper(),
                    "name": account.name or account.code,
                }

        def is_revenue_account(account_code: str) -> bool:
            """Check if account is a revenue account."""
            info = account_info.get(account_code, {})
            account_type = info.get("type", "")
            account_class = info.get("class", "")
            return (
                "REVENUE" in account_type
                or "REVENUE" in account_class
                or "INCOME" in account_type
                or "SALES" in account_type
            )

        def is_expense_account(account_code: str) -> bool:
            """Check if account is an expense account."""
            info = account_info.get(account_code, {})
            account_type = info.get("type", "")
            account_class = info.get("class", "")
            # Note: DEPRECIATN is a contra-asset (balance sheet), not an expense
            return (
                "EXPENSE" in account_type
                or "EXPENSE" in account_class
                or "EXP" in account_type
                or "DIRECTCOSTS" in account_type
                or "OVERHEADS" in account_type
            )

        total_income = 0.0
        total_expenses = 0.0

        # Process invoices - extract P&L impact from line items
        for invoice in invoices:
            if invoice.status not in ("AUTHORISED", "PAID"):
                continue
            try:
                invoice_date = datetime.strptime(invoice.date, "%Y-%m-%d")
                if not (start_date <= invoice_date <= end_date):
                    continue
            except (ValueError, AttributeError, TypeError):
                continue

            try:
                line_items = json.loads(invoice.line_items) if invoice.line_items else []
                if not isinstance(line_items, list):
                    line_items = []
            except (json.JSONDecodeError, TypeError):
                line_items = []

            for item in line_items:
                if not isinstance(item, dict):
                    continue
                account_code = item.get("AccountCode", "")
                if not account_code:
                    continue
                try:
                    line_amount = float(item.get("LineAmount", 0) or 0)
                except (ValueError, TypeError):
                    continue
                if line_amount == 0:
                    continue
                description = item.get("Description") or f"Invoice {invoice.invoice_number}"

                if invoice.type == "ACCREC" and is_revenue_account(account_code):
                    total_income += line_amount
                    income_rows.append(
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": description}, {"Value": f"{line_amount:.2f}"}],
                        }
                    )
                elif invoice.type == "ACCPAY" and is_expense_account(account_code):
                    total_expenses += line_amount
                    expense_rows.append(
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": description}, {"Value": f"{line_amount:.2f}"}],
                        }
                    )

        # Process bank transactions
        for txn in bank_transactions:
            if txn.status != "AUTHORISED":
                continue
            txn_date_str = txn.date
            if not txn_date_str:
                continue
            try:
                if "T" in txn_date_str:
                    txn_date_str = txn_date_str.split("T")[0]
                txn_date = datetime.strptime(txn_date_str, "%Y-%m-%d")
                if not (start_date <= txn_date <= end_date):
                    continue
            except (ValueError, AttributeError, TypeError):
                continue

            try:
                line_items = json.loads(txn.line_items) if txn.line_items else []
            except (json.JSONDecodeError, TypeError):
                line_items = []
            if not line_items:
                continue

            for item in line_items:
                if not isinstance(item, dict):
                    continue
                account_code = item.get("AccountCode", "")
                try:
                    line_amount = float(item.get("LineAmount", 0) or 0)
                except (ValueError, TypeError):
                    continue
                if line_amount == 0:
                    continue
                description = item.get("Description") or "Bank Transaction"

                if txn.type == "SPEND" and is_expense_account(account_code):
                    total_expenses += line_amount
                    expense_rows.append(
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": description}, {"Value": f"{line_amount:.2f}"}],
                        }
                    )
                elif txn.type == "RECEIVE" and is_revenue_account(account_code):
                    total_income += line_amount
                    income_rows.append(
                        {
                            "RowType": "Row",
                            "Cells": [{"Value": description}, {"Value": f"{line_amount:.2f}"}],
                        }
                    )

    # Calculate profit
    gross_profit = total_income - total_expenses
    net_profit = gross_profit  # Simplified - in real system would account for tax, etc.

    # Build P&L report structure
    report = {
        "ReportID": "ProfitAndLoss",
        "ReportName": "Profit and Loss",
        "ReportType": "ProfitAndLoss",
        "ReportTitles": ["Profit and Loss", "Demo Company (US)", formatted_date_range],
        "ReportDate": formatted_date_range,
        "UpdatedDateUTC": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "Rows": [
            {"RowType": "Header", "Cells": [{"Value": "Account"}, {"Value": "Amount"}]},
            {
                "RowType": "Section",
                "Title": "Income",
                "Rows": income_rows
                + [
                    {
                        "RowType": "SummaryRow",
                        "Cells": [{"Value": "Total Income"}, {"Value": f"{total_income:.2f}"}],
                    }
                ],
            },
            {
                "RowType": "Section",
                "Title": "Expenses",
                "Rows": expense_rows
                + [
                    {
                        "RowType": "SummaryRow",
                        "Cells": [{"Value": "Total Expenses"}, {"Value": f"{total_expenses:.2f}"}],
                    }
                ],
            },
            {
                "RowType": "SummaryRow",
                "Cells": [{"Value": "Gross Profit"}, {"Value": f"{gross_profit:.2f}"}],
            },
            {
                "RowType": "SummaryRow",
                "Cells": [{"Value": "Net Profit"}, {"Value": f"{net_profit:.2f}"}],
            },
        ],
    }

    logger.info(
        f"P&L Summary - Income: {total_income:.2f}, Expenses: {total_expenses:.2f}, Net Profit: {net_profit:.2f}"
    )

    response = {"Reports": [report]}
    report_period = {"fromDate": from_date, "toDate": to_date}
    return self._add_metadata(response, "xero-mock", "offline", report_period=report_period)


async def get_report_aged_receivables(
    self,
    contact_id: str,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """
    Get aged receivables report for a specific contact.

    This report shows outstanding AR invoices grouped by aging buckets
    (Current, 30, 60, 90+ days overdue).

    Args:
        contact_id: Contact UUID (required) - only show invoices for this contact
        date: Report date (YYYY-MM-DD) - shows payments up to this date.
              Defaults to end of current month.
        from_date: Show invoices from this date (YYYY-MM-DD)
        to_date: Show invoices to this date (YYYY-MM-DD)

    Returns:
        Aged receivables report with metadata

    Raises:
        ValueError: If date format is invalid
    """
    logger.info(
        f"Generating aged receivables report for contact: {contact_id}, "
        f"date={date}, from_date={from_date}, to_date={to_date}"
    )

    # Determine report date (defaults to end of current month)
    if date:
        normalized_date = normalize_xero_date(date)
        try:
            report_date = datetime.strptime(normalized_date, "%Y-%m-%d")
        except ValueError as err:
            logger.error(f"Invalid date format: {date}")
            raise ValueError(
                f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got date='{date}'"
            ) from err
    else:
        # Default to end of current month
        today = datetime.now()
        last_day = monthrange(today.year, today.month)[1]
        report_date = datetime(today.year, today.month, last_day)

    # Parse from_date and to_date if provided
    parsed_from_date = None
    parsed_to_date = None

    if from_date:
        normalized_from = normalize_xero_date(from_date)
        try:
            parsed_from_date = datetime.strptime(normalized_from, "%Y-%m-%d")
        except ValueError as err:
            logger.error(f"Invalid from_date format: {from_date}")
            raise ValueError(
                f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got from_date='{from_date}'"
            ) from err

    if to_date:
        normalized_to = normalize_xero_date(to_date)
        try:
            parsed_to_date = datetime.strptime(normalized_to, "%Y-%m-%d")
        except ValueError as err:
            logger.error(f"Invalid to_date format: {to_date}")
            raise ValueError(
                f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got to_date='{to_date}'"
            ) from err

    # Format dates for display
    formatted_report_date = report_date.strftime("%d %B %Y")
    report_date_str = report_date.strftime("%Y-%m-%d")

    # Query invoices from database
    async with async_session() as session:
        query = select(Invoice)
        result = await session.execute(query)
        invoices = result.scalars().all()

    # Filter invoices:
    # 1. Must be ACCREC (accounts receivable) type
    # 2. Must belong to specified contact
    # 3. Must not be DRAFT, VOIDED, or DELETED status
    # 4. Must have outstanding amount (AmountDue > 0)
    # 5. Apply date range filters if specified

    valid_statuses = {"AUTHORISED", "PAID", "SUBMITTED"}
    filtered_invoices = []

    for invoice in invoices:
        # Check type
        if invoice.type != "ACCREC":
            continue

        # Check contact
        contact_data = json.loads(invoice.contact) if invoice.contact else {}
        invoice_contact_id = contact_data.get("ContactID", "")
        if invoice_contact_id != contact_id:
            continue

        # Check status
        if invoice.status not in valid_statuses:
            continue

        # Check if invoice has outstanding amount
        amount_due = invoice.amount_due or 0.0
        if amount_due <= 0:
            continue

        # Apply date filters - if date filters are specified, invoice must have a date
        if parsed_from_date or parsed_to_date:
            if not invoice.date:
                # Skip invoices without dates when date filtering is requested
                continue
            try:
                invoice_date = datetime.strptime(invoice.date, "%Y-%m-%d")
            except ValueError:
                continue

            if parsed_from_date and invoice_date < parsed_from_date:
                continue
            if parsed_to_date and invoice_date > parsed_to_date:
                continue

        filtered_invoices.append(invoice)

    # Build invoice rows for the report with aging buckets
    invoice_rows = []
    total_amount = 0.0
    total_paid = 0.0
    total_credited = 0.0
    total_due = 0.0

    # Aging bucket totals
    bucket_current = 0.0
    bucket_30 = 0.0
    bucket_60 = 0.0
    bucket_90_plus = 0.0

    def calculate_aging_bucket(
        due_date_str: str, amount: float, as_of_date: datetime
    ) -> tuple[float, float, float, float]:
        """Calculate which aging bucket an amount belongs to.

        Returns tuple of (current, 30, 60, 90+) with amount in appropriate bucket.
        Negative amounts (overpaid/credit balances) are excluded from aging buckets.
        """
        if amount <= 0:
            # Exclude negative amounts (overpaid invoices, credit balances) from aging
            return (0.0, 0.0, 0.0, 0.0)
        if not due_date_str:
            return (amount, 0.0, 0.0, 0.0)  # Default to current if no due date

        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
        except ValueError:
            return (amount, 0.0, 0.0, 0.0)

        days_overdue = (as_of_date - due_date).days

        if days_overdue <= 0:
            return (amount, 0.0, 0.0, 0.0)  # Current (not yet due)
        elif days_overdue <= 30:
            return (0.0, amount, 0.0, 0.0)  # 1-30 days overdue
        elif days_overdue <= 60:
            return (0.0, 0.0, amount, 0.0)  # 31-60 days overdue
        else:
            return (0.0, 0.0, 0.0, amount)  # 61+ days overdue

    for invoice in filtered_invoices:
        invoice_date = invoice.date or ""
        due_date = invoice.due_date or ""
        reference = invoice.invoice_number or ""
        total = invoice.total or 0.0
        paid = invoice.amount_paid or 0.0
        credited = invoice.amount_credited or 0.0
        due = invoice.amount_due or 0.0

        # Calculate aging bucket for this invoice
        current, d30, d60, d90_plus = calculate_aging_bucket(due_date, due, report_date)
        bucket_current += current
        bucket_30 += d30
        bucket_60 += d60
        bucket_90_plus += d90_plus

        # Accumulate totals
        total_amount += total
        total_paid += paid
        total_credited += credited
        total_due += due

        invoice_row = {
            "RowType": "Row",
            "Cells": [
                {"Value": invoice_date},
                {"Value": reference},
                {"Value": due_date},
                {"Value": f"{current:.2f}"},
                {"Value": f"{d30:.2f}"},
                {"Value": f"{d60:.2f}"},
                {"Value": f"{d90_plus:.2f}"},
                {"Value": f"{due:.2f}"},
            ],
        }
        invoice_rows.append(invoice_row)

    # Build summary row with aging bucket totals
    summary_row = {
        "RowType": "SummaryRow",
        "Cells": [
            {"Value": "Total"},
            {"Value": ""},
            {"Value": ""},
            {"Value": f"{bucket_current:.2f}"},
            {"Value": f"{bucket_30:.2f}"},
            {"Value": f"{bucket_60:.2f}"},
            {"Value": f"{bucket_90_plus:.2f}"},
            {"Value": f"{total_due:.2f}"},
        ],
    }

    # Build report titles
    report_titles = [
        "Aged Receivables",
        "Demo Company (US)",
        f"As at {formatted_report_date}",
        f"Showing payments to {formatted_report_date}",
    ]

    # Build report structure with aging bucket headers
    report = {
        "ReportID": "AgedReceivablesByContact",
        "ReportName": "Aged Receivables By Contact",
        "ReportType": "AgedReceivablesByContact",
        "ReportTitles": report_titles,
        "ReportDate": formatted_report_date,
        "UpdatedDateUTC": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "Rows": [
            {
                "RowType": "Header",
                "Cells": [
                    {"Value": "Date"},
                    {"Value": "Reference"},
                    {"Value": "Due Date"},
                    {"Value": "Current"},
                    {"Value": "1-30 Days"},
                    {"Value": "31-60 Days"},
                    {"Value": "61+ Days"},
                    {"Value": "Total Due"},
                ],
            },
            {
                "RowType": "Section",
                "Rows": invoice_rows + [summary_row] if invoice_rows else [],
            },
        ],
    }

    logger.info(
        f"Aged receivables report - Contact: {contact_id}, "
        f"Invoices: {len(filtered_invoices)}, Total Due: {total_due:.2f}, "
        f"Buckets: Current={bucket_current:.2f}, 30={bucket_30:.2f}, "
        f"60={bucket_60:.2f}, 61+={bucket_90_plus:.2f}"
    )

    response = {"Reports": [report]}

    # Build report period metadata
    report_period: dict[str, Any] = {"asOfDate": report_date_str}
    if from_date:
        report_period["fromDate"] = from_date
    if to_date:
        report_period["toDate"] = to_date

    return self._add_metadata(response, "xero-mock", "offline", report_period=report_period)


async def get_report_aged_payables(
    self,
    contact_id: str,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """
    Get aged payables report for a specific contact (supplier).

    This report shows outstanding AP bills (ACCPAY invoices) grouped by aging buckets
    (Current, 30, 60, 90+ days overdue).

    Args:
        contact_id: Contact UUID (required) - only show bills for this contact
        date: Report date (YYYY-MM-DD) - shows payments up to this date.
              Defaults to end of current month.
        from_date: Show bills from this date (YYYY-MM-DD)
        to_date: Show bills to this date (YYYY-MM-DD)

    Returns:
        Aged payables report with metadata

    Raises:
        ValueError: If date format is invalid
    """
    logger.info(
        f"Generating aged payables report for contact: {contact_id}, "
        f"date={date}, from_date={from_date}, to_date={to_date}"
    )

    # Determine report date (defaults to end of current month)
    if date:
        normalized_date = normalize_xero_date(date)
        try:
            report_date = datetime.strptime(normalized_date, "%Y-%m-%d")
        except ValueError as err:
            logger.error(f"Invalid date format: {date}")
            raise ValueError(
                f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got date='{date}'"
            ) from err
    else:
        # Default to end of current month
        today = datetime.now()
        last_day = monthrange(today.year, today.month)[1]
        report_date = datetime(today.year, today.month, last_day)

    # Parse from_date and to_date if provided
    parsed_from_date = None
    parsed_to_date = None

    if from_date:
        normalized_from = normalize_xero_date(from_date)
        try:
            parsed_from_date = datetime.strptime(normalized_from, "%Y-%m-%d")
        except ValueError as err:
            logger.error(f"Invalid from_date format: {from_date}")
            raise ValueError(
                f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got from_date='{from_date}'"
            ) from err

    if to_date:
        normalized_to = normalize_xero_date(to_date)
        try:
            parsed_to_date = datetime.strptime(normalized_to, "%Y-%m-%d")
        except ValueError as err:
            logger.error(f"Invalid to_date format: {to_date}")
            raise ValueError(
                f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got to_date='{to_date}'"
            ) from err

    # Format dates for display
    formatted_report_date = report_date.strftime("%d %B %Y")
    report_date_str = report_date.strftime("%Y-%m-%d")

    # Query invoices from database
    async with async_session() as session:
        query = select(Invoice)
        result = await session.execute(query)
        invoices = result.scalars().all()

    # Filter invoices:
    # 1. Must be ACCPAY (accounts payable) type - bills from suppliers
    # 2. Must belong to specified contact
    # 3. Must not be DRAFT, VOIDED, or DELETED status
    # 4. Must have outstanding amount (AmountDue > 0)
    # 5. Apply date range filters if specified

    valid_statuses = {"AUTHORISED", "PAID", "SUBMITTED"}
    filtered_invoices = []

    for invoice in invoices:
        # Check type - must be ACCPAY (bills/payables)
        if invoice.type != "ACCPAY":
            continue

        # Check contact
        contact_data = json.loads(invoice.contact) if invoice.contact else {}
        invoice_contact_id = contact_data.get("ContactID", "")
        if invoice_contact_id != contact_id:
            continue

        # Check status
        if invoice.status not in valid_statuses:
            continue

        # Check if invoice has outstanding amount
        amount_due = invoice.amount_due or 0.0
        if amount_due <= 0:
            continue

        # Apply date filters - if date filters are specified, invoice must have a date
        if parsed_from_date or parsed_to_date:
            if not invoice.date:
                # Skip invoices without dates when date filtering is requested
                continue
            try:
                invoice_date = datetime.strptime(invoice.date, "%Y-%m-%d")
            except ValueError:
                continue

            if parsed_from_date and invoice_date < parsed_from_date:
                continue
            if parsed_to_date and invoice_date > parsed_to_date:
                continue

        filtered_invoices.append(invoice)

    # Build bill rows for the report with aging buckets
    bill_rows = []
    total_amount = 0.0
    total_paid = 0.0
    total_credited = 0.0
    total_due = 0.0

    # Aging bucket totals
    bucket_current = 0.0
    bucket_30 = 0.0
    bucket_60 = 0.0
    bucket_90_plus = 0.0

    def calculate_aging_bucket(
        due_date_str: str, amount: float, as_of_date: datetime
    ) -> tuple[float, float, float, float]:
        """Calculate which aging bucket an amount belongs to.

        Returns tuple of (current, 30, 60, 90+) with amount in appropriate bucket.
        Negative amounts (overpaid/credit balances) are excluded from aging buckets.
        """
        if amount <= 0:
            # Exclude negative amounts (overpaid invoices, credit balances) from aging
            return (0.0, 0.0, 0.0, 0.0)
        if not due_date_str:
            return (amount, 0.0, 0.0, 0.0)  # Default to current if no due date

        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
        except ValueError:
            return (amount, 0.0, 0.0, 0.0)

        days_overdue = (as_of_date - due_date).days

        if days_overdue <= 0:
            return (amount, 0.0, 0.0, 0.0)  # Current (not yet due)
        elif days_overdue <= 30:
            return (0.0, amount, 0.0, 0.0)  # 1-30 days overdue
        elif days_overdue <= 60:
            return (0.0, 0.0, amount, 0.0)  # 31-60 days overdue
        else:
            return (0.0, 0.0, 0.0, amount)  # 61+ days overdue

    for invoice in filtered_invoices:
        invoice_date = invoice.date or ""
        due_date = invoice.due_date or ""
        reference = invoice.invoice_number or ""
        total = invoice.total or 0.0
        paid = invoice.amount_paid or 0.0
        credited = invoice.amount_credited or 0.0
        due = invoice.amount_due or 0.0

        # Calculate aging bucket for this bill
        current, d30, d60, d90_plus = calculate_aging_bucket(due_date, due, report_date)
        bucket_current += current
        bucket_30 += d30
        bucket_60 += d60
        bucket_90_plus += d90_plus

        # Accumulate totals
        total_amount += total
        total_paid += paid
        total_credited += credited
        total_due += due

        bill_row = {
            "RowType": "Row",
            "Cells": [
                {"Value": invoice_date},
                {"Value": reference},
                {"Value": due_date},
                {"Value": f"{current:.2f}"},
                {"Value": f"{d30:.2f}"},
                {"Value": f"{d60:.2f}"},
                {"Value": f"{d90_plus:.2f}"},
                {"Value": f"{due:.2f}"},
            ],
        }
        bill_rows.append(bill_row)

    # Build summary row with aging bucket totals
    summary_row = {
        "RowType": "SummaryRow",
        "Cells": [
            {"Value": "Total"},
            {"Value": ""},
            {"Value": ""},
            {"Value": f"{bucket_current:.2f}"},
            {"Value": f"{bucket_30:.2f}"},
            {"Value": f"{bucket_60:.2f}"},
            {"Value": f"{bucket_90_plus:.2f}"},
            {"Value": f"{total_due:.2f}"},
        ],
    }

    # Build report titles
    report_titles = [
        "Aged Payables",
        "Demo Company (US)",
        f"As at {formatted_report_date}",
        f"Showing payments to {formatted_report_date}",
    ]

    # Build report structure with aging bucket headers
    report = {
        "ReportID": "AgedPayablesByContact",
        "ReportName": "Aged Payables By Contact",
        "ReportType": "AgedPayablesByContact",
        "ReportTitles": report_titles,
        "ReportDate": formatted_report_date,
        "UpdatedDateUTC": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "Rows": [
            {
                "RowType": "Header",
                "Cells": [
                    {"Value": "Date"},
                    {"Value": "Reference"},
                    {"Value": "Due Date"},
                    {"Value": "Current"},
                    {"Value": "1-30 Days"},
                    {"Value": "31-60 Days"},
                    {"Value": "61+ Days"},
                    {"Value": "Total Due"},
                ],
            },
            {
                "RowType": "Section",
                "Rows": bill_rows + [summary_row] if bill_rows else [],
            },
        ],
    }

    logger.info(
        f"Aged payables report - Contact: {contact_id}, "
        f"Bills: {len(filtered_invoices)}, Total Due: {total_due:.2f}, "
        f"Buckets: Current={bucket_current:.2f}, 30={bucket_30:.2f}, "
        f"60={bucket_60:.2f}, 61+={bucket_90_plus:.2f}"
    )

    response = {"Reports": [report]}

    # Build report period metadata
    report_period: dict[str, Any] = {"asOfDate": report_date_str}
    if from_date:
        report_period["fromDate"] = from_date
    if to_date:
        report_period["toDate"] = to_date

    return self._add_metadata(response, "xero-mock", "offline", report_period=report_period)


async def get_report_executive_summary(
    self,
    date: str,
) -> dict[str, Any]:
    """
    Get executive summary report with KPIs and trends.

    The executive summary provides a high-level view of the organization's
    financial health including Cash, Receivables, and Payables sections
    with trend data.

    Args:
        date: Report date (YYYY-MM-DD) - required parameter

    Returns:
        Executive summary report with metadata including:
        - Reports array with report structure
        - Cash section with bank balances
        - Receivables section with AR totals
        - Payables section with AP totals
        - Profitability/trend section with comparison data
        - meta block with mode, provider, calledAt, reportPeriod

    Raises:
        ValueError: If date format is invalid or empty
    """
    logger.info(f"Generating executive summary report for date: {date}")

    # Validate date - it's required and must be in YYYY-MM-DD format
    if not date or not date.strip():
        raise ValueError("Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got empty date")

    # Normalize date to handle various formats (e.g., 2024/01/01 -> 2024-01-01)
    normalized_date = normalize_xero_date(date)
    try:
        report_date = datetime.strptime(normalized_date, "%Y-%m-%d")
        formatted_date = report_date.strftime("%d %B %Y")
    except ValueError as err:
        logger.error(f"Invalid date format: {date}")
        raise ValueError(
            f"Invalid date format. Expected YYYY-MM-DD (or YYYY/MM/DD), got date='{date}'"
        ) from err

    # Query data from database for KPIs
    async with async_session() as session:
        # Get all accounts for cash balances
        account_result = await session.execute(select(Account))
        accounts = account_result.scalars().all()

        # Get all invoices for AR/AP totals
        invoice_result = await session.execute(select(Invoice))
        invoices = invoice_result.scalars().all()

        # Get all bank transactions for cash calculation
        bank_txn_result = await session.execute(select(BankTransaction))
        bank_transactions = bank_txn_result.scalars().all()

    # Calculate Cash using shared function (includes ALL bank transactions)
    total_cash_in_bank = _calculate_cash_balance(accounts, bank_transactions, report_date)

    # Calculate Receivables section KPIs using shared function
    total_receivables = _calculate_total_ar(invoices, report_date)

    # Calculate overdue receivables separately
    # Must apply same invoice date filter as _calculate_total_ar for consistency
    overdue_receivables = 0.0
    for invoice in invoices:
        if invoice.type != "ACCREC":
            continue
        if invoice.status not in ("AUTHORISED", "SUBMITTED", "PAID"):
            continue
        # Filter by invoice date (same as _calculate_total_ar)
        if invoice.date:
            try:
                invoice_date = datetime.strptime(normalize_xero_date(invoice.date), "%Y-%m-%d")
                if invoice_date > report_date:
                    continue
            except ValueError:
                pass
        amount_due = invoice.amount_due or 0.0
        if amount_due > 0 and invoice.due_date:
            try:
                due_date = datetime.strptime(invoice.due_date, "%Y-%m-%d")
                if due_date < report_date:
                    overdue_receivables += amount_due
            except ValueError:
                pass

    # Calculate Payables section KPIs using shared function
    total_payables = _calculate_total_ap(invoices, report_date)

    # Calculate overdue payables separately
    # Must apply same invoice date filter as _calculate_total_ap for consistency
    overdue_payables = 0.0
    for invoice in invoices:
        if invoice.type != "ACCPAY":
            continue
        if invoice.status not in ("AUTHORISED", "SUBMITTED", "PAID"):
            continue
        # Filter by invoice date (same as _calculate_total_ap)
        if invoice.date:
            try:
                invoice_date = datetime.strptime(normalize_xero_date(invoice.date), "%Y-%m-%d")
                if invoice_date > report_date:
                    continue
            except ValueError:
                pass
        amount_due = invoice.amount_due or 0.0
        if amount_due > 0 and invoice.due_date:
            try:
                due_date = datetime.strptime(invoice.due_date, "%Y-%m-%d")
                if due_date < report_date:
                    overdue_payables += amount_due
            except ValueError:
                pass

    # Calculate income/expense for profitability trend
    # Filter invoices to the report month for accurate "This Month" KPIs
    report_year = report_date.year
    report_month = report_date.month

    # Calculate previous month for comparison
    if report_month == 1:
        prev_year, prev_month = report_year - 1, 12
    else:
        prev_year, prev_month = report_year, report_month - 1

    this_month_income = 0.0
    this_month_expenses = 0.0
    prev_month_income = 0.0
    prev_month_expenses = 0.0

    for invoice in invoices:
        if invoice.status not in ("AUTHORISED", "PAID"):
            continue
        # Use sub_total (excludes tax) for P&L calculations
        # Tax is a balance sheet item, not income/expense
        amount = invoice.sub_total or 0.0

        # Parse invoice date to determine which month it belongs to
        invoice_year, invoice_month = None, None
        if invoice.date:
            try:
                inv_date = datetime.strptime(invoice.date, "%Y-%m-%d")
                invoice_year, invoice_month = inv_date.year, inv_date.month
            except ValueError:
                pass

        # Classify invoice into current or previous month
        is_this_month = invoice_year == report_year and invoice_month == report_month
        is_prev_month = invoice_year == prev_year and invoice_month == prev_month

        if invoice.type == "ACCREC":
            if is_this_month:
                this_month_income += amount
            elif is_prev_month:
                prev_month_income += amount
        elif invoice.type == "ACCPAY":
            if is_this_month:
                this_month_expenses += amount
            elif is_prev_month:
                prev_month_expenses += amount

    net_profit = this_month_income - this_month_expenses
    previous_net_profit = prev_month_income - prev_month_expenses
    # Use abs() in denominator to handle negative profits correctly:
    # When profit goes from -92 to -100, we want to show -8.7% (worsening),
    # not +8.7% which would incorrectly suggest improvement
    profit_change_pct = (
        ((net_profit - previous_net_profit) / abs(previous_net_profit) * 100)
        if previous_net_profit
        else 0
    )

    # Build Cash section
    cash_section = {
        "RowType": "Section",
        "Title": "Cash",
        "Rows": [
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Cash in Bank"},
                    {"Value": f"{total_cash_in_bank:.2f}"},
                ],
            },
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Cash This Month"},
                    {"Value": f"{total_cash_in_bank * 0.15:.2f}"},  # Mock monthly cash flow
                ],
            },
            {
                "RowType": "SummaryRow",
                "Cells": [
                    {"Value": "Total Cash"},
                    {"Value": f"{total_cash_in_bank:.2f}"},
                ],
            },
        ],
    }

    # Build Receivables section
    receivables_section = {
        "RowType": "Section",
        "Title": "Receivables",
        "Rows": [
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Accounts Receivable"},
                    {"Value": f"{total_receivables:.2f}"},
                ],
            },
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Overdue Receivables"},
                    {"Value": f"{overdue_receivables:.2f}"},
                ],
            },
            {
                "RowType": "SummaryRow",
                "Cells": [
                    {"Value": "Total Receivables"},
                    {"Value": f"{total_receivables:.2f}"},
                ],
            },
        ],
    }

    # Build Payables section
    payables_section = {
        "RowType": "Section",
        "Title": "Payables",
        "Rows": [
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Accounts Payable"},
                    {"Value": f"{total_payables:.2f}"},
                ],
            },
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Overdue Payables"},
                    {"Value": f"{overdue_payables:.2f}"},
                ],
            },
            {
                "RowType": "SummaryRow",
                "Cells": [
                    {"Value": "Total Payables"},
                    {"Value": f"{total_payables:.2f}"},
                ],
            },
        ],
    }

    # Build Profitability/Trend section
    profitability_section = {
        "RowType": "Section",
        "Title": "Profitability",
        "Rows": [
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "This Month Net Profit"},
                    {"Value": f"{net_profit:.2f}"},
                ],
            },
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Last Month Net Profit"},
                    {"Value": f"{previous_net_profit:.2f}"},
                ],
            },
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Change %"},
                    {"Value": f"{profit_change_pct:.1f}%"},
                ],
            },
            {
                "RowType": "SummaryRow",
                "Cells": [
                    {"Value": "Net Profit"},
                    {"Value": f"{net_profit:.2f}"},
                ],
            },
        ],
    }

    # Calculate Tax Position
    tax_position = _calculate_tax_position(invoices, report_date)

    # Build Tax section
    tax_section = {
        "RowType": "Section",
        "Title": "Tax Position",
        "Rows": [
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "GST/VAT Collected"},
                    {"Value": f"{tax_position['tax_collected']:.2f}"},
                ],
            },
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "GST/VAT Paid"},
                    {"Value": f"{tax_position['tax_paid']:.2f}"},
                ],
            },
            {
                "RowType": "SummaryRow",
                "Cells": [
                    {"Value": "Net Tax Liability"},
                    {"Value": f"{tax_position['net_tax_liability']:.2f}"},
                ],
            },
        ],
    }

    # Calculate Financial Ratios
    # Note: Gross Margin requires COGS data which we don't have separately from operating expenses.
    # We only calculate Net Margin since we can't distinguish COGS from other expenses.
    # Net margin = Net Profit / Income
    net_margin = net_profit / this_month_income * 100 if this_month_income > 0 else 0.0
    # Working capital = Current Assets - Current Liabilities (simplified: Cash + AR - AP)
    working_capital = total_cash_in_bank + total_receivables - total_payables
    # Current ratio = Current Assets / Current Liabilities
    current_ratio = (
        (total_cash_in_bank + total_receivables) / total_payables if total_payables > 0 else None
    )

    # Build Financial Ratios section
    # Note: Gross Margin omitted because we can't distinguish COGS from operating expenses
    ratios_rows = [
        {
            "RowType": "Row",
            "Cells": [
                {"Value": "Net Margin"},
                {"Value": f"{net_margin:.1f}%"},
            ],
        },
        {
            "RowType": "Row",
            "Cells": [
                {"Value": "Working Capital"},
                {"Value": f"{working_capital:.2f}"},
            ],
        },
    ]

    # Only add current ratio if calculable (not div by zero)
    if current_ratio is not None:
        ratios_rows.append(
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": "Current Ratio"},
                    {"Value": f"{current_ratio:.2f}"},
                ],
            }
        )

    ratios_section = {
        "RowType": "Section",
        "Title": "Financial Ratios",
        "Rows": ratios_rows,
    }

    # Use a fixed UpdatedDateUTC based on report date for idempotency
    updated_date_utc = report_date.strftime("%Y-%m-%dT00:00:00") + "Z"

    # Build report structure
    report = {
        "ReportID": "ExecutiveSummary",
        "ReportName": "Executive Summary",
        "ReportType": "ExecutiveSummary",
        "ReportTitles": [
            "Executive Summary",
            "Demo Company (US)",
            f"As at {formatted_date}",
        ],
        "ReportDate": formatted_date,
        "UpdatedDateUTC": updated_date_utc,
        "Rows": [
            cash_section,
            receivables_section,
            payables_section,
            profitability_section,
            tax_section,
            ratios_section,
        ],
    }

    logger.info(
        f"Executive summary generated - Cash: {total_cash_in_bank:.2f}, "
        f"Receivables: {total_receivables:.2f}, Payables: {total_payables:.2f}, "
        f"Net Profit: {net_profit:.2f}"
    )

    response = {"Reports": [report]}
    report_period = {"asOfDate": date}

    return self._add_metadata(response, "xero-mock", "offline", report_period=report_period)


async def get_budget_summary(
    self,
    date: str | None = None,
    periods: int | None = None,
    timeframe: int | None = None,
) -> dict[str, Any]:
    """
    Get budget vs actual comparison report from database.

    The report compares budgeted amounts vs actual amounts by account,
    calculating variance (Actual - Budget) for each account.

    Args:
        date: Report date (YYYY-MM-DD) e.g. 2014-04-30.
              Defaults to today's date if not specified.
        periods: Number of periods to compare (1-12). Default is 1.
        timeframe: Period size - 1=month, 3=quarter, 12=year. Default is 1.

    Returns:
        Budget summary report with metadata including:
        - Reports array with report structure
        - Header row with Account, Budget, Actual, Variance columns
        - Data rows showing budget vs actual for each account
        - meta block with mode, provider, calledAt

    Raises:
        ValueError: If date format is invalid (not YYYY-MM-DD)
    """
    from calendar import monthrange

    logger.info(
        f"Generating budget summary from database "
        f"(date={date}, periods={periods}, timeframe={timeframe})"
    )

    # Validate and parse date if provided
    if date:
        try:
            report_date = datetime.strptime(date, "%Y-%m-%d")
            report_date_str = date
        except ValueError as err:
            logger.error(f"Invalid date format: {date}")
            raise ValueError(
                f"Invalid date format. Expected YYYY-MM-DD, got date='{date}'"
            ) from err
    else:
        # Default to end of current month
        today = datetime.now()
        last_day = monthrange(today.year, today.month)[1]
        report_date = datetime(today.year, today.month, last_day)
        report_date_str = report_date.strftime("%Y-%m-%d")

    # Format date for display
    formatted_report_date = report_date.strftime("%d %B %Y")

    # Use defaults for periods and timeframe if not specified
    effective_periods = periods if periods is not None else 1
    effective_timeframe = timeframe if timeframe is not None else 1

    # Query budgets and accounts from database to generate report
    async with async_session() as session:
        # Get all accounts
        account_result = await session.execute(select(Account))
        accounts = account_result.scalars().all()

        # Get all invoices (for actual amounts - these represent P&L impact)
        invoice_result = await session.execute(select(Invoice))
        invoices = invoice_result.scalars().all()

        # Get all bank transactions
        bank_txn_result = await session.execute(select(BankTransaction))
        bank_transactions = bank_txn_result.scalars().all()

    # Build account lookup by code
    account_by_code: dict[str, Any] = {}
    for account in accounts:
        if account.code:
            account_by_code[account.code] = {
                "name": account.name,
                "type": (account.type or "").upper(),
            }

    # Calculate actual amounts from invoices by account type
    # For revenue accounts: ACCREC invoices (sales)
    # For expense accounts: ACCPAY invoices (bills)
    actual_by_account: dict[str, float] = {}

    for invoice in invoices:
        # Only include finalized invoices
        if invoice.status not in ("AUTHORISED", "PAID"):
            continue

        # Parse line items to get account codes
        try:
            line_items = json.loads(invoice.line_items) if invoice.line_items else []
            if not isinstance(line_items, list):
                line_items = []
        except (json.JSONDecodeError, TypeError):
            continue

        for item in line_items:
            if not isinstance(item, dict):
                continue
            account_code = item.get("AccountCode", "")
            if not account_code or account_code not in account_by_code:
                continue

            try:
                line_amount = float(item.get("LineAmount", 0) or 0)
            except (ValueError, TypeError):
                continue

            if line_amount == 0:
                continue

            account_name = account_by_code[account_code]["name"]
            actual_by_account[account_name] = actual_by_account.get(account_name, 0.0) + line_amount

    # Also include bank transactions for actuals
    for txn in bank_transactions:
        if txn.status != "AUTHORISED":
            continue

        try:
            line_items = json.loads(txn.line_items) if txn.line_items else []
            if not isinstance(line_items, list):
                line_items = []
        except (json.JSONDecodeError, TypeError):
            continue

        for item in line_items:
            if not isinstance(item, dict):
                continue
            account_code = item.get("AccountCode", "")
            if not account_code or account_code not in account_by_code:
                continue

            try:
                line_amount = float(item.get("LineAmount", 0) or 0)
            except (ValueError, TypeError):
                continue

            if line_amount == 0:
                continue

            account_name = account_by_code[account_code]["name"]
            actual_by_account[account_name] = actual_by_account.get(account_name, 0.0) + line_amount

    # Generate budget data - in a real system this would come from budget entities
    # For synthetic data purposes, we generate representative budget vs actual data
    # This creates realistic budget data based on actual amounts with variance

    # Define representative budget entries
    budget_data = [
        {"account": "Sales Revenue", "budget": 120000.00},
        {"account": "Service Revenue", "budget": 80000.00},
        {"account": "Cost of Goods Sold", "budget": 45000.00},
        {"account": "Salaries", "budget": 60000.00},
        {"account": "Rent", "budget": 24000.00},
        {"account": "Utilities", "budget": 6000.00},
        {"account": "Office Supplies", "budget": 3000.00},
        {"account": "Marketing", "budget": 15000.00},
    ]

    # Build data rows
    data_rows = []
    for entry in budget_data:
        account_name = entry["account"]
        budget_amount = entry["budget"]
        # Get actual from calculated actuals, or generate synthetic actual
        # with some variance from budget for realistic reporting
        actual_amount = actual_by_account.get(
            account_name,
            budget_amount * (0.9 + (hash(account_name) % 20) / 100),  # 90-110% of budget
        )

        # Variance = Actual - Budget
        variance = actual_amount - budget_amount

        data_rows.append(
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": account_name},
                    {"Value": f"{budget_amount:.2f}"},
                    {"Value": f"{actual_amount:.2f}"},
                    {"Value": f"{variance:.2f}"},
                ],
            }
        )

    # Build report structure matching Xero API format
    # Use a fixed UpdatedDateUTC based on report date for idempotency
    updated_date_utc = report_date.strftime("%Y-%m-%dT00:00:00") + "Z"

    report = {
        "ReportID": "BudgetSummary",
        "ReportName": "Budget Summary",
        "ReportType": "BudgetSummary",
        "ReportTitles": [
            "Budget Summary",
            "Demo Company (US)",
            f"For the period ended {formatted_report_date}",
        ],
        "ReportDate": formatted_report_date,
        "UpdatedDateUTC": updated_date_utc,
        "Rows": [
            {
                "RowType": "Header",
                "Cells": [
                    {"Value": "Account"},
                    {"Value": "Budget"},
                    {"Value": "Actual"},
                    {"Value": "Variance"},
                ],
            },
            *data_rows,
        ],
    }

    logger.info(
        f"Budget summary generated - {len(data_rows)} account rows, "
        f"periods={effective_periods}, timeframe={effective_timeframe}"
    )

    response = {"Reports": [report]}
    report_period = {"asOfDate": report_date_str}

    return self._add_metadata(response, "xero-mock", "offline", report_period=report_period)
