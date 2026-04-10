"""QuickBooks Field Check Verifier - Flexible dropdown + value interface.

This verifier provides a simple dropdown interface for domain experts to check
any QuickBooks field without writing SQL. The dropdown selection maps to
pre-defined SQL queries that have been verified against the actual QB schema.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.evals.quickbooks.utils import parse_date
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# All queries are 100% schema-accurate
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # Account-related checks
    "Account Count": {
        "query": "SELECT COUNT(*) FROM accounts WHERE active = 1",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of active accounts",
    },
    "Account Count All": {
        "query": "SELECT COUNT(*) FROM accounts",
        "required_params": [],
        "result_type": "integer",
        "description": "Count all accounts (including inactive)",
    },
    "Account Count by Type": {
        "query": "SELECT COUNT(*) FROM accounts WHERE account_type = :account_type AND active = 1",
        "required_params": ["account_type"],
        "result_type": "integer",
        "description": "Count active accounts by AccountType (Asset, Liability, Equity, Income, Expense)",
    },
    "Account Count by Classification": {
        "query": "SELECT COUNT(*) FROM accounts WHERE classification = :classification AND active = 1",
        "required_params": ["classification"],
        "result_type": "integer",
        "description": "Count active accounts by Classification (Asset, Liability, Equity, Revenue, Expense)",
    },
    "Account Balance": {
        "query": "SELECT current_balance FROM accounts WHERE name = :account_name AND active = 1",
        "required_params": ["account_name"],
        "result_type": "decimal",
        "description": "Check the current balance of an account",
    },
    "Account Exists": {
        "query": "SELECT COUNT(*) FROM accounts WHERE name = :account_name",
        "required_params": ["account_name"],
        "result_type": "boolean",
        "description": "Verify an account exists in the chart of accounts",
    },
    "Account Classification": {
        "query": "SELECT classification FROM accounts WHERE name = :account_name",
        "required_params": ["account_name"],
        "result_type": "string",
        "description": "Check account classification (Asset, Liability, Equity, Revenue, Expense)",
    },
    # Customer-related checks
    "Customer Balance": {
        "query": "SELECT balance FROM customers WHERE display_name = :customer_name",
        "required_params": ["customer_name"],
        "result_type": "decimal",
        "description": "Check customer's outstanding balance",
    },
    "Customer Exists": {
        "query": "SELECT COUNT(*) FROM customers WHERE display_name = :customer_name",
        "required_params": ["customer_name"],
        "result_type": "boolean",
        "description": "Verify customer exists",
    },
    # Invoice-related checks
    "Invoice Count": {
        "query": "SELECT COUNT(*) FROM invoices",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of invoices",
    },
    "Customer Invoice Count": {
        "query": "SELECT COUNT(*) FROM invoices WHERE customer_id = :customer_id",
        "required_params": ["customer_id"],
        "result_type": "integer",
        "description": "Count invoices for a specific customer",
    },
    "Invoice Total Amount": {
        "query": "SELECT total_amount FROM invoices WHERE doc_number = :invoice_number",
        "required_params": ["invoice_number"],
        "result_type": "decimal",
        "description": "Check total amount of a specific invoice",
    },
    "Invoice Balance": {
        "query": "SELECT balance FROM invoices WHERE doc_number = :invoice_number",
        "required_params": ["invoice_number"],
        "result_type": "decimal",
        "description": "Check outstanding balance on an invoice",
    },
    "Invoice Due Date": {
        "query": "SELECT due_date FROM invoices WHERE doc_number = :invoice_number",
        "required_params": ["invoice_number"],
        "result_type": "string",
        "description": "Get due date of a specific invoice",
    },
    "Invoice DocNumber by ID": {
        "query": "SELECT doc_number FROM invoices WHERE id = :invoice_id",
        "required_params": ["invoice_id"],
        "result_type": "string",
        "description": "Get DocNumber for a specific invoice by ID",
    },
    "Invoice Due Date by ID": {
        "query": "SELECT due_date FROM invoices WHERE id = :invoice_id",
        "required_params": ["invoice_id"],
        "result_type": "string",
        "description": "Get due date for a specific invoice by ID",
    },
    "Invoice Balance by ID": {
        "query": "SELECT balance FROM invoices WHERE id = :invoice_id",
        "required_params": ["invoice_id"],
        "result_type": "decimal",
        "description": "Get balance for a specific invoice by ID",
    },
    "Invoice Total Amount by ID": {
        "query": "SELECT total_amount FROM invoices WHERE id = :invoice_id",
        "required_params": ["invoice_id"],
        "result_type": "decimal",
        "description": "Get total amount for a specific invoice by ID",
    },
    "Invoice Status by ID": {
        "query": "SELECT status FROM invoices WHERE id = :invoice_id",
        "required_params": ["invoice_id"],
        "result_type": "string",
        "description": "Get status for a specific invoice by ID",
    },
    "Total AR Balance": {
        "query": "SELECT COALESCE(SUM(balance), 0) FROM invoices WHERE status != 'Paid'",
        "required_params": [],
        "result_type": "decimal",
        "description": "Calculate total Accounts Receivable balance (sum of unpaid invoice balances)",
    },
    # Account Balance and P&L Calculation checks
    "Account Balance by Name": {
        "query": "SELECT current_balance FROM accounts WHERE name = :account_name",
        "required_params": ["account_name"],
        "result_type": "decimal",
        "description": "Get current balance of an account by account name",
    },
    "Expense by Account Name": {
        "query": """
            SELECT
                COALESCE(SUM(CASE WHEN jel.posting_type = 'Debit' THEN jel.amount ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN jel.posting_type = 'Credit' THEN jel.amount ELSE 0 END), 0)
            FROM journal_entry_lines jel
            JOIN accounts a ON jel.account_id = a.id
            JOIN journal_entries je ON jel.journal_entry_id = je.id
            WHERE a.name = :account_name
            AND a.classification = 'Expense'
            AND (je.doc_number IS NULL OR je.doc_number NOT LIKE 'JE-CLOSE%')
        """,
        "required_params": ["account_name"],
        "result_type": "decimal",
        "description": "Sum total expenses for a specific expense account (debits - credits)",
    },
    "Revenue by Account Name": {
        "query": """
            SELECT
                COALESCE(SUM(CASE WHEN jel.posting_type = 'Credit' THEN jel.amount ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN jel.posting_type = 'Debit' THEN jel.amount ELSE 0 END), 0)
            FROM journal_entry_lines jel
            JOIN accounts a ON jel.account_id = a.id
            JOIN journal_entries je ON jel.journal_entry_id = je.id
            WHERE a.name = :account_name
            AND a.classification IN ('Revenue', 'Income')
            AND (je.doc_number IS NULL OR je.doc_number NOT LIKE 'JE-CLOSE%')
        """,
        "required_params": ["account_name"],
        "result_type": "decimal",
        "description": "Sum total revenue for a specific income account (credits - debits)",
    },
    "Net Income": {
        "query": """
            SELECT
                COALESCE(SUM(CASE
                    WHEN a.classification IN ('Revenue', 'Income') AND jel.posting_type = 'Credit' THEN jel.amount
                    WHEN a.classification IN ('Revenue', 'Income') AND jel.posting_type = 'Debit' THEN -jel.amount
                    ELSE 0
                END), 0) -
                COALESCE(SUM(CASE
                    WHEN a.classification = 'Expense' AND jel.posting_type = 'Debit' THEN jel.amount
                    WHEN a.classification = 'Expense' AND jel.posting_type = 'Credit' THEN -jel.amount
                    ELSE 0
                END), 0)
            FROM journal_entry_lines jel
            JOIN accounts a ON jel.account_id = a.id
            JOIN journal_entries je ON jel.journal_entry_id = je.id
            WHERE (je.doc_number IS NULL OR je.doc_number NOT LIKE 'JE-CLOSE%')
        """,
        "required_params": [],
        "result_type": "decimal",
        "description": "Calculate net income (revenue - expenses) from journal entries (properly handles posting types)",
    },
    # Journal Entry-related checks
    "Journal Entry Count": {
        "query": "SELECT COUNT(*) FROM journal_entries",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of journal entries",
    },
    "Journal Entry Count by Date": {
        "query": "SELECT COUNT(*) FROM journal_entries WHERE txn_date BETWEEN :start_date AND :end_date",
        "required_params": ["start_date", "end_date"],
        "result_type": "integer",
        "description": "Count journal entries in a date range",
    },
    "Journal Entry Total Amount": {
        "query": "SELECT total_amt FROM journal_entries WHERE doc_number = :doc_number",
        "required_params": ["doc_number"],
        "result_type": "decimal",
        "description": "Check total amount of a journal entry",
    },
    # FIXED: Split into two separate queries for clarity
    "Journal Entry by Doc Number": {
        "query": "SELECT COUNT(*) FROM journal_entries WHERE doc_number = :doc_number",
        "required_params": ["doc_number"],
        "result_type": "boolean",
        "description": "Verify journal entry exists by doc number",
    },
    "Journal Entry by Description": {
        "query": "SELECT COUNT(*) FROM journal_entries WHERE private_note LIKE '%' || :description || '%'",
        "required_params": ["description"],
        "result_type": "boolean",
        "description": "Verify journal entry exists by description (fuzzy match)",
    },
    # Bill-related checks
    "Bill Count All": {
        "query": "SELECT COUNT(*) FROM bill",
        "required_params": [],
        "result_type": "integer",
        "description": "Count all bills",
    },
    "Bill Count by Vendor": {
        "query": "SELECT COUNT(*) FROM bill WHERE vendor_ref_id = :vendor_id",
        "required_params": ["vendor_id"],
        "result_type": "integer",
        "description": "Count bills for a specific vendor",
    },
    "Bill Balance": {
        "query": "SELECT balance FROM bill WHERE doc_number = :bill_number",
        "required_params": ["bill_number"],
        "result_type": "decimal",
        "description": "Check outstanding balance on a bill",
    },
    # Payment-related checks
    "Payment Count": {
        "query": "SELECT COUNT(*) FROM payments",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of payments",
    },
    "Customer Payment Count": {
        "query": "SELECT COUNT(*) FROM payments WHERE customer_ref_value = :customer_id",
        "required_params": ["customer_id"],
        "result_type": "integer",
        "description": "Count payments from a specific customer",
    },
    "Payment Total": {
        "query": "SELECT total_amt FROM payments WHERE id = :payment_id",
        "required_params": ["payment_id"],
        "result_type": "decimal",
        "description": "Check total amount of a payment",
    },
    # Bank Transaction checks
    "Bank Transaction Count": {
        "query": "SELECT COUNT(*) FROM bank_transaction WHERE bank_id = :bank_id AND status = :status",
        "required_params": ["bank_id", "status"],
        "result_type": "integer",
        "description": "Count bank transactions by status (pending, matched, reconciled)",
    },
    # Aggregation checks
    "Total Revenue": {
        "query": """
            SELECT COALESCE(SUM(
                CASE WHEN jel.posting_type = 'Credit' THEN jel.amount
                     WHEN jel.posting_type = 'Debit' THEN -jel.amount
                     ELSE 0 END
            ), 0)
            FROM journal_entry_lines jel
            JOIN accounts a ON jel.account_id = a.id
            JOIN journal_entries je ON jel.journal_entry_id = je.id
            WHERE a.classification IN ('Revenue', 'Income') AND a.active = 1
            AND (je.doc_number IS NULL OR je.doc_number NOT LIKE 'JE-CLOSE%')
        """,
        "required_params": [],
        "result_type": "decimal",
        "description": "Calculate total revenue from all revenue accounts",
    },
    "Total Expenses": {
        "query": """
            SELECT COALESCE(SUM(
                CASE WHEN jel.posting_type = 'Debit' THEN jel.amount
                     WHEN jel.posting_type = 'Credit' THEN -jel.amount
                     ELSE 0 END
            ), 0)
            FROM journal_entry_lines jel
            JOIN accounts a ON jel.account_id = a.id
            JOIN journal_entries je ON jel.journal_entry_id = je.id
            WHERE a.classification = 'Expense' AND a.active = 1
            AND (je.doc_number IS NULL OR je.doc_number NOT LIKE 'JE-CLOSE%')
        """,
        "required_params": [],
        "result_type": "decimal",
        "description": "Calculate total expenses from all expense accounts",
    },
}


async def quickbooks_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any QuickBooks field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    # Validate helper results available
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    # Get field selection
    field_to_check = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="field_to_check is required",
        )

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    # Get field configuration
    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Unknown field_to_check: {field_to_check}",
        )

    # Extract required parameters from verifier_values
    params = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Required parameter '{param}' is missing",
            )
        params[param] = value

    # Get database connection from snapshot_dbs helper
    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
    if not dbs:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="No databases found in snapshot",
        )

    # Filter to QuickBooks databases only (avoid picking up ADP, Xero, etc.)
    qb_dbs = [
        (alias, db_info)
        for alias, db_info in dbs.items()
        if "quickbooks" in alias.lower()
    ]
    if len(qb_dbs) != 1:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Expected exactly 1 QuickBooks database, found {len(qb_dbs)}. Available: {list(dbs.keys())}",
        )

    _alias, db_info = qb_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="QuickBooks database found but connection is None",
        )

    # Execute query
    try:
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"QuickBooks field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Query execution failed: {str(e)}",
        )

    # Compare based on result_type
    result_type = field_config["result_type"]

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "decimal":
        # Get tolerance with validation (only needed for decimal comparisons)
        try:
            tolerance_value = input.verifier.verifier_values.get("tolerance", 0.01)
            if tolerance_value is None:
                tolerance_value = 0.01
            tolerance = Decimal(str(tolerance_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid tolerance value: {str(e)}",
            )

        try:
            result_decimal = Decimal(str(result))
            expected_decimal = Decimal(str(expected_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {str(e)}",
            )
        matches = abs(result_decimal - expected_decimal) <= tolerance
        message = f"{field_to_check}: ${float(result_decimal):,.2f} {'✓' if matches else f'✗ expected ${float(expected_decimal):,.2f}'}"
    elif result_type == "boolean":
        # Convert COUNT results to boolean
        actual_bool = result > 0
        expected_bool = str(expected_value).lower() in ["true", "1", "yes"]
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Exists' if actual_bool else 'Does not exist'} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            matches = int(result) == int(expected_value)
        except (ValueError, TypeError) as e:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {str(e)}",
            )
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    elif result_type == "string":
        # First try exact case-insensitive match
        matches = str(result).lower() == str(expected_value).lower()

        # If no exact match, try parsing as dates (handles "2025-09-05 00:00:00" vs "2025-09-05")
        if not matches:
            result_date = parse_date(str(result))
            expected_date = parse_date(str(expected_value))
            if result_date is not None and expected_date is not None:
                matches = result_date.date() == expected_date.date()

        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if matches else 0.0,
        verifier_result_values={
            "actual_value": str(result) if result is not None else None,
            "expected_value": str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
        },
        message=message,
    )
