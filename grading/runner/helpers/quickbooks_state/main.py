"""QuickBooks State Helper - Parses QB database from snapshot."""

import io
import os
import sqlite3
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict

from loguru import logger

from runner.models import AgentTrajectoryOutput

# Possible locations for QuickBooks database in snapshot
QUICKBOOKS_DB_PATHS = [
    ".apps_data/quickbooks/data.db",  # RL Studio standard
    "quickbooks/data.db",
    "quickbooks.db",
    "quickbooks_data.db",  # Legacy
]


def _find_quickbooks_db(zip_file: zipfile.ZipFile) -> str | None:
    """Find QuickBooks database in snapshot.

    Searches for the database in known locations, avoiding other app databases
    like Xero that might also be in the snapshot.
    """
    all_files = zip_file.namelist()
    logger.debug(f"Snapshot contains {len(all_files)} files")

    # First: check explicit known paths
    for path in QUICKBOOKS_DB_PATHS:
        if path in all_files:
            logger.info(f"Found QuickBooks database at: {path}")
            return path
        # Check suffix match for different root prefixes
        matching = [f for f in all_files if f.endswith(path)]
        if matching:
            logger.info(f"Found QuickBooks database at: {matching[0]}")
            return matching[0]

    # Last resort: any .db file with quickbooks in path
    qb_dbs = [f for f in all_files if "quickbooks" in f.lower() and f.endswith(".db")]
    if qb_dbs:
        logger.info(f"Found QuickBooks database via search: {qb_dbs[0]}")
        return qb_dbs[0]

    logger.warning(f"No QuickBooks database found. Searched: {QUICKBOOKS_DB_PATHS}")
    return None


# ====================
# Type Definitions
# ====================


class JournalEntryLine(TypedDict):
    """Structure for a journal entry line (debit or credit)."""

    account: str
    amount: Decimal
    description: str


class JournalEntry(TypedDict):
    """Structure for a complete journal entry."""

    id: str
    doc_number: str
    txn_date: str
    description: str
    debits: list[JournalEntryLine]
    credits: list[JournalEntryLine]


# ====================
# DB Extraction Utils
# ====================


def _safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Safely convert a value to Decimal, returning default on failure."""
    if value is None:
        return default
    try:
        str_value = str(value).strip()
        if not str_value:
            return default
        return Decimal(str_value)
    except (ValueError, TypeError, InvalidOperation):
        return default


def extract_pnl_from_db(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Extract P&L from accounts + journal_entry_lines.
    Returns: {"Total Revenue": 232500, "Wage Expenses": 106500, ...}
    """
    cursor = conn.cursor()
    accounts = cursor.execute("""
        SELECT id, name, classification
        FROM accounts
        WHERE active = 1 AND classification IN ('Revenue', 'Income', 'Expense')
    """).fetchall()

    pnl_data = {}
    total_revenue = Decimal("0")
    total_expense = Decimal("0")

    for account in accounts:
        balance_query = """
            SELECT
                COALESCE(SUM(CASE WHEN jel.posting_type = 'Debit' THEN jel.amount ELSE 0 END), 0) as debits,
                COALESCE(SUM(CASE WHEN jel.posting_type = 'Credit' THEN jel.amount ELSE 0 END), 0) as credits
            FROM journal_entry_lines jel
            JOIN journal_entries je ON jel.journal_entry_id = je.id
            WHERE jel.account_id = ?
              AND (je.doc_number IS NULL OR je.doc_number NOT LIKE 'JE-CLOSE%')
        """
        result = cursor.execute(balance_query, (account["id"],)).fetchone()
        debits = _safe_decimal(result["debits"])
        credits = _safe_decimal(result["credits"])

        # Revenue: credits increase | Expense: debits increase
        is_revenue = account["classification"] in ("Revenue", "Income")
        balance = (credits - debits) if is_revenue else (debits - credits)
        pnl_data[account["name"]] = balance

        # Accumulate totals
        if is_revenue:
            total_revenue += balance
        else:  # Expense
            total_expense += balance

    # Add calculated totals (standard P&L report format)
    pnl_data["Total Income"] = total_revenue
    pnl_data["Total Revenue"] = total_revenue  # Alias
    pnl_data["Total Expense"] = total_expense
    pnl_data["Net Income"] = total_revenue - total_expense
    pnl_data["Net Profit"] = total_revenue - total_expense  # Alias

    # Log extracted accounts for debugging
    logger.info(
        f"P&L extracted {len(pnl_data)} line items: {list(pnl_data.keys())[:20]}"
    )

    return pnl_data


def extract_balance_sheet_from_db(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Extract Balance Sheet from accounts.
    Returns: {"Cash": 50000, "Line of Credit": 300000, ...}
    """
    cursor = conn.cursor()
    accounts = cursor.execute("""
        SELECT id, name, classification
        FROM accounts
        WHERE active = 1 AND classification IN ('Asset', 'Liability', 'Equity')
    """).fetchall()

    bs_data = {}
    total_assets = Decimal("0")
    total_liabilities = Decimal("0")
    total_equity = Decimal("0")

    for account in accounts:
        balance_query = """
            SELECT
                COALESCE(SUM(CASE WHEN posting_type = 'Debit' THEN amount ELSE 0 END), 0) as debits,
                COALESCE(SUM(CASE WHEN posting_type = 'Credit' THEN amount ELSE 0 END), 0) as credits
            FROM journal_entry_lines WHERE account_id = ?
        """
        result = cursor.execute(balance_query, (account["id"],)).fetchone()
        debits = _safe_decimal(result["debits"])
        credits = _safe_decimal(result["credits"])

        # Asset: debits increase | Liability/Equity: credits increase
        balance = (
            (debits - credits)
            if account["classification"] == "Asset"
            else (credits - debits)
        )
        bs_data[account["name"]] = balance

        # Accumulate totals
        if account["classification"] == "Asset":
            total_assets += balance
        elif account["classification"] == "Liability":
            total_liabilities += balance
        else:  # Equity
            total_equity += balance

    # Add calculated totals (standard Balance Sheet format)
    bs_data["Total Assets"] = total_assets
    bs_data["Total Liabilities"] = total_liabilities
    bs_data["Total Equity"] = total_equity

    return bs_data


def extract_journal_entries_with_lines(
    conn: sqlite3.Connection,
) -> list[JournalEntry]:
    """Extract JEs with DR/CR lines.
    Returns: [{"id": "je_001", "debits": [...], "credits": [...]}, ...]
    """
    cursor = conn.cursor()
    entries = cursor.execute("""
        SELECT id, doc_number, txn_date, private_note
        FROM journal_entries ORDER BY txn_date, id
    """).fetchall()

    result = []
    for entry in entries:
        lines = cursor.execute(
            """
            SELECT jel.posting_type, jel.amount, jel.description, a.name as account_name
            FROM journal_entry_lines jel
            JOIN accounts a ON jel.account_id = a.id
            WHERE jel.journal_entry_id = ?
            ORDER BY jel.line_number
        """,
            (entry["id"],),
        ).fetchall()

        debits = [
            {
                "account": line["account_name"],
                "amount": _safe_decimal(line["amount"]),
                "description": line["description"],
            }
            for line in lines
            if line["posting_type"] == "Debit"
        ]
        credits = [
            {
                "account": line["account_name"],
                "amount": _safe_decimal(line["amount"]),
                "description": line["description"],
            }
            for line in lines
            if line["posting_type"] == "Credit"
        ]

        result.append(
            {
                "id": entry["id"],
                "doc_number": entry["doc_number"],
                "txn_date": str(entry["txn_date"]),
                "description": entry["private_note"],
                "debits": debits,
                "credits": credits,
            }
        )

    return result


# ====================
# Main Helper
# ====================


async def quickbooks_state_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
) -> dict[str, Any]:
    """Parse QuickBooks database from final snapshot.

    Extracts QuickBooks database and parses accounting-specific data.

    Returns parsed financial reports and journal entries.
    """
    # Reset BytesIO position for reading
    final_snapshot_bytes.seek(0)

    with zipfile.ZipFile(final_snapshot_bytes, "r") as final_zip:
        # Find QuickBooks database specifically (not just any .db file)
        db_file = _find_quickbooks_db(final_zip)

        if not db_file:
            logger.warning("No QuickBooks database found in snapshot")
            return {"pnl_report": {}, "balance_sheet": {}, "journal_entries": []}

        db_bytes = final_zip.read(db_file)

        # Write to temp file (SQLite needs file path)
        temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False, mode="wb")
        temp_file_path = temp_file.name

        try:
            temp_file.write(db_bytes)
            temp_file.flush()
            temp_file.close()

            # Create SQLite connection
            conn = sqlite3.connect(temp_file_path)
            conn.row_factory = sqlite3.Row

            logger.info(f"Parsing QuickBooks data from database: {db_file}")

            try:
                result = {
                    "pnl_report": extract_pnl_from_db(conn),
                    "balance_sheet": extract_balance_sheet_from_db(conn),
                    "journal_entries": extract_journal_entries_with_lines(conn),
                }
            finally:
                conn.close()
        finally:
            # Clean up temp file
            try:
                os.unlink(temp_file_path)
            except OSError as e:
                logger.warning(f"Failed to delete temp file {temp_file_path}: {e}")

    # Reset BytesIO position after use for potential reuse
    final_snapshot_bytes.seek(0)

    return result
