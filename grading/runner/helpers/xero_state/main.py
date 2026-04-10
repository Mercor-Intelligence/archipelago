"""Xero State Helper - Extracts Xero data from snapshot database.

Data Flow:
---------
1. mercor-xero stores data in SQLite at {APPS_DATA_ROOT}/xero/data.db
2. Snapshot includes the database at .apps_data/xero/data.db
3. This helper extracts the db, queries tables, converts to Xero API format

Database Schema (SQLite) → Xero API Format:
------------------------------------------
Each table uses snake_case columns, converted to PascalCase for API:
- accounts: account_id → AccountID, code → Code, name → Name, etc.
- contacts: contact_id → ContactID, is_supplier → IsSupplier, etc.
- invoices: invoice_id → InvoiceID, contact (JSON) → Contact (dict), etc.
- bank_transactions: bank_transaction_id → BankTransactionID, etc.
- payments: payment_id → PaymentID, invoice_id → InvoiceID, etc.

JSON fields (contact, line_items, addresses, phones) are parsed from TEXT.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from runner.models import AgentTrajectoryOutput

# Possible locations for Xero database in snapshot
XERO_DB_PATHS = [
    ".apps_data/xero/data.db",  # RL Studio standard
    "xero/data.db",
    "xero_data.db",  # Legacy
    # Note: Do NOT include generic "data.db" - it would match other apps' databases
]


def _find_xero_db(zip_file: zipfile.ZipFile) -> str | None:
    """Find Xero database in snapshot."""
    all_files = zip_file.namelist()
    logger.debug(f"Snapshot contains {len(all_files)} files")

    for path in XERO_DB_PATHS:
        if path in all_files:
            logger.info(f"Found Xero database at: {path}")
            return path
        # Check suffix match for different root prefixes
        matching = [f for f in all_files if f.endswith(path)]
        if matching:
            logger.info(f"Found Xero database at: {matching[0]}")
            return matching[0]

    # Last resort: any .db file with xero in path
    xero_dbs = [f for f in all_files if "xero" in f.lower() and f.endswith(".db")]
    if xero_dbs:
        logger.info(f"Found Xero database via search: {xero_dbs[0]}")
        return xero_dbs[0]

    logger.warning(f"No Xero database found. Searched: {XERO_DB_PATHS}")
    return None


def _parse_json(value: str | None) -> dict[str, Any] | list[Any]:
    """Parse JSON string, return empty dict on failure."""
    if not value:
        return {}
    try:
        result = json.loads(value)
        if isinstance(result, dict | list):
            return result
        return {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Extract accounts → Xero API format (matches Account.to_dict())."""
    cursor = conn.execute("SELECT * FROM accounts")
    columns: list[str] = [desc[0] for desc in cursor.description]
    result: list[dict[str, Any]] = []

    for row in cursor.fetchall():
        row_tuple: tuple[Any, ...] = row
        r: dict[str, Any] = dict(zip(columns, row_tuple, strict=False))
        result.append(
            {
                "AccountID": r.get("account_id"),
                "Code": r.get("code"),
                "Name": r.get("name"),
                "Status": r.get("status"),
                "Type": r.get("type"),
                "TaxType": r.get("tax_type"),
                "Class": r.get("class"),
                "CurrencyCode": r.get("currency_code"),
                "BankAccountNumber": r.get("bank_account_number"),
                "AccountBalance": r.get("account_balance"),
            }
        )

    return result


def _extract_contacts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Extract contacts → Xero API format (matches Contact.to_dict())."""
    cursor = conn.execute("SELECT * FROM contacts")
    columns: list[str] = [desc[0] for desc in cursor.description]
    result: list[dict[str, Any]] = []

    for row in cursor.fetchall():
        row_tuple: tuple[Any, ...] = row
        r: dict[str, Any] = dict(zip(columns, row_tuple, strict=False))
        addresses = _parse_json(r.get("addresses"))
        phones = _parse_json(r.get("phones"))
        result.append(
            {
                "ContactID": r.get("contact_id"),
                "Name": r.get("name"),
                "EmailAddress": r.get("email_address"),
                "ContactStatus": r.get("contact_status"),
                "IsSupplier": r.get("is_supplier"),
                "IsCustomer": r.get("is_customer"),
                "Addresses": addresses if isinstance(addresses, list) else [],
                "Phones": phones if isinstance(phones, list) else [],
            }
        )

    return result


def _extract_invoices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Extract invoices → Xero API format (matches Invoice.to_dict()).

    Note: AmountDue/AmountPaid may not be in DB; default to Total/0 for compatibility
    with verifier calculations that use these fields.
    """
    cursor = conn.execute("SELECT * FROM invoices")
    columns: list[str] = [desc[0] for desc in cursor.description]
    result: list[dict[str, Any]] = []

    for row in cursor.fetchall():
        row_tuple: tuple[Any, ...] = row
        r: dict[str, Any] = dict(zip(columns, row_tuple, strict=False))
        contact = _parse_json(r.get("contact"))
        line_items = _parse_json(r.get("line_items"))
        total = r.get("total")

        # AmountDue defaults to Total if not stored (for AR aging calculations)
        amount_due = r.get("amount_due") if r.get("amount_due") is not None else total
        amount_paid = r.get("amount_paid") if r.get("amount_paid") is not None else 0

        result.append(
            {
                "InvoiceID": r.get("invoice_id"),
                "InvoiceNumber": r.get("invoice_number"),
                "Type": r.get("type"),
                "Status": r.get("status"),
                "Date": r.get("date"),
                "DueDate": r.get("due_date"),
                "Contact": contact if isinstance(contact, dict) else {},
                "LineItems": line_items if isinstance(line_items, list) else [],
                "SubTotal": r.get("sub_total"),
                "TotalTax": r.get("total_tax"),
                "Total": total,
                "AmountDue": amount_due,
                "AmountPaid": amount_paid,
                "CurrencyCode": r.get("currency_code"),
            }
        )

    return result


def _extract_bank_transactions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Extract bank_transactions → Xero API format (matches BankTransaction.to_dict())."""
    cursor = conn.execute("SELECT * FROM bank_transactions")
    columns: list[str] = [desc[0] for desc in cursor.description]
    result: list[dict[str, Any]] = []

    for row in cursor.fetchall():
        row_tuple: tuple[Any, ...] = row
        r: dict[str, Any] = dict(zip(columns, row_tuple, strict=False))
        contact = _parse_json(r.get("contact"))
        line_items = _parse_json(r.get("line_items"))
        result.append(
            {
                "BankTransactionID": r.get("bank_transaction_id"),
                "Type": r.get("type"),
                "Status": r.get("status"),
                "Date": r.get("date"),
                "Contact": contact if isinstance(contact, dict) else {},
                "LineItems": line_items if isinstance(line_items, list) else [],
                "SubTotal": r.get("sub_total"),
                "TotalTax": r.get("total_tax"),
                "Total": r.get("total"),
            }
        )

    return result


def _extract_payments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Extract payments → Xero API format (matches Payment.to_dict())."""
    cursor = conn.execute("SELECT * FROM payments")
    columns: list[str] = [desc[0] for desc in cursor.description]
    result: list[dict[str, Any]] = []

    for row in cursor.fetchall():
        row_tuple: tuple[Any, ...] = row
        r: dict[str, Any] = dict(zip(columns, row_tuple, strict=False))
        result.append(
            {
                "PaymentID": r.get("payment_id"),
                "InvoiceID": r.get("invoice_id"),
                "Date": r.get("date"),
                "Amount": r.get("amount"),
                "CurrencyCode": r.get("currency_code"),
                "Status": r.get("status"),
            }
        )

    return result


def _extract_all(db_content: bytes) -> dict[str, list[dict[str, Any]]]:
    """Extract all Xero entities from database."""
    result: dict[str, list[dict[str, Any]]] = {
        "accounts": [],
        "bank_transactions": [],
        "contacts": [],
        "invoices": [],
        "payments": [],
    }

    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
        _ = tmp.write(db_content)
        tmp.flush()

        try:
            conn = sqlite3.connect(tmp.name)

            # Extract each entity, handling missing tables
            extractors: list[
                tuple[str, Callable[[sqlite3.Connection], list[dict[str, Any]]]]
            ] = [
                ("accounts", _extract_accounts),
                ("contacts", _extract_contacts),
                ("invoices", _extract_invoices),
                ("bank_transactions", _extract_bank_transactions),
                ("payments", _extract_payments),
            ]

            for key, extractor in extractors:
                try:
                    result[key] = extractor(conn)
                except sqlite3.OperationalError as e:
                    logger.warning(f"Could not extract {key}: {e}")

            conn.close()

        except sqlite3.DatabaseError as e:
            logger.error(f"Failed to open database: {e}")

    return result


async def xero_state_helper(
    initial_snapshot_bytes: io.BytesIO,  # noqa: ARG001
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,  # noqa: ARG001
) -> dict[str, list[dict[str, Any]]]:
    """Extract Xero state from final snapshot database.

    Reads the SQLite database from the snapshot and extracts all entities
    in Xero API format (PascalCase keys, matching the to_dict() methods).

    Expected location: .apps_data/xero/data.db

    Returns:
        Dictionary with Xero state:
        - accounts: List[dict] - Chart of accounts
        - bank_transactions: List[dict] - Cash transactions
        - contacts: List[dict] - Customers/suppliers
        - invoices: List[dict] - AR/AP invoices
        - payments: List[dict] - Payment records
    """
    _ = final_snapshot_bytes.seek(0)

    result: dict[str, list[dict[str, Any]]] = {
        "accounts": [],
        "bank_transactions": [],
        "contacts": [],
        "invoices": [],
        "payments": [],
    }

    try:
        with zipfile.ZipFile(final_snapshot_bytes, "r") as final_zip:
            db_path = _find_xero_db(final_zip)

            if db_path:
                db_content = final_zip.read(db_path)
                result = _extract_all(db_content)

                logger.info(
                    f"Loaded Xero state: {len(result['accounts'])} accounts, "
                    f"{len(result['contacts'])} contacts, "
                    f"{len(result['invoices'])} invoices, "
                    f"{len(result['bank_transactions'])} transactions, "
                    f"{len(result['payments'])} payments"
                )
            else:
                logger.warning("No Xero database found in snapshot")

    except zipfile.BadZipFile as e:
        logger.error(f"Invalid snapshot zip: {e}")
    except Exception as e:
        logger.error(f"Error extracting Xero state: {e}")

    _ = final_snapshot_bytes.seek(0)
    return result
