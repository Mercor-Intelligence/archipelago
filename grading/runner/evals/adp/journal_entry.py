"""ADP Journal Entry Verifier - Validates journal entry balance and line items.

This verifier provides two main functions:
1. Balance Check: Verify that debits = credits (fundamental accounting rule)
2. Line Item Check: Verify specific lines exist with correct amounts

Schema Reference (from mercor-adp/mcp_servers/adp_payroll/db/models/journal_entry.py):
- journal_entries: id, run_id, posting_date, status, total_debits, total_credits, memo
- journal_entry_lines: id, je_id, line_number, account_code, account_name,
                       debit_amount, credit_amount, cost_center, department, description

Note: Uses je_id as foreign key, not journal_entry_id
Note: Uses debit_amount/credit_amount, not simply debit/credit
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# Check type configurations
CHECK_TYPES: dict[str, dict[str, Any]] = {
    "Balance Check": {
        "description": "Verify total debits = total credits (from header)",
        "header_query": "SELECT total_debits, total_credits FROM journal_entries WHERE id = :je_id",
        "required_params": ["je_id"],
    },
    "Lines Balance Check": {
        "description": "Verify sum of debit_amount = sum of credit_amount (from lines)",
        "lines_query": """
            SELECT
                COALESCE(SUM(debit_amount), 0) as total_debits,
                COALESCE(SUM(credit_amount), 0) as total_credits
            FROM journal_entry_lines
            WHERE je_id = :je_id
        """,
        "required_params": ["je_id"],
    },
    "Line Debit Amount": {
        "description": "Verify a specific line has expected debit_amount",
        "query": """
            SELECT debit_amount
            FROM journal_entry_lines
            WHERE je_id = :je_id AND account_code = :account_code
        """,
        "required_params": ["je_id", "account_code"],
        "value_field": "debit_amount",
    },
    "Line Credit Amount": {
        "description": "Verify a specific line has expected credit_amount",
        "query": """
            SELECT credit_amount
            FROM journal_entry_lines
            WHERE je_id = :je_id AND account_code = :account_code
        """,
        "required_params": ["je_id", "account_code"],
        "value_field": "credit_amount",
    },
    "Line Exists": {
        "description": "Check if a line with specific account_code exists",
        "query": """
            SELECT COUNT(*)
            FROM journal_entry_lines
            WHERE je_id = :je_id AND account_code = :account_code
        """,
        "required_params": ["je_id", "account_code"],
        "result_type": "boolean",
    },
    "Line Count": {
        "description": "Count lines in a journal entry",
        "query": "SELECT COUNT(*) FROM journal_entry_lines WHERE je_id = :je_id",
        "required_params": ["je_id"],
        "result_type": "integer",
    },
    "Account Total Debits": {
        "description": "Sum of debit_amount for a specific account across all lines",
        "query": """
            SELECT COALESCE(SUM(debit_amount), 0)
            FROM journal_entry_lines
            WHERE je_id = :je_id AND account_code = :account_code
        """,
        "required_params": ["je_id", "account_code"],
    },
    "Account Total Credits": {
        "description": "Sum of credit_amount for a specific account across all lines",
        "query": """
            SELECT COALESCE(SUM(credit_amount), 0)
            FROM journal_entry_lines
            WHERE je_id = :je_id AND account_code = :account_code
        """,
        "required_params": ["je_id", "account_code"],
    },
    "Department Total Debits": {
        "description": "Sum of debit_amount for a specific department",
        "query": """
            SELECT COALESCE(SUM(debit_amount), 0)
            FROM journal_entry_lines
            WHERE je_id = :je_id AND department = :department
        """,
        "required_params": ["je_id", "department"],
    },
    "Department Total Credits": {
        "description": "Sum of credit_amount for a specific department",
        "query": """
            SELECT COALESCE(SUM(credit_amount), 0)
            FROM journal_entry_lines
            WHERE je_id = :je_id AND department = :department
        """,
        "required_params": ["je_id", "department"],
    },
    "Cost Center Total Debits": {
        "description": "Sum of debit_amount for a specific cost center",
        "query": """
            SELECT COALESCE(SUM(debit_amount), 0)
            FROM journal_entry_lines
            WHERE je_id = :je_id AND cost_center = :cost_center
        """,
        "required_params": ["je_id", "cost_center"],
    },
    "Cost Center Total Credits": {
        "description": "Sum of credit_amount for a specific cost center",
        "query": """
            SELECT COALESCE(SUM(credit_amount), 0)
            FROM journal_entry_lines
            WHERE je_id = :je_id AND cost_center = :cost_center
        """,
        "required_params": ["je_id", "cost_center"],
    },
}


async def adp_journal_entry_eval(input: EvalImplInput) -> VerifierResult:
    """Verify journal entry balance and line items.

    Supports:
    - Balance verification (debits = credits)
    - Specific line item checks by account
    - Aggregation by department/cost center
    """
    # Validate helper results available
    if not input.helper_results:
        raise ValueError("Helper results not available")

    # Get check type
    check_type_raw = input.verifier.verifier_values.get("check_type")
    if not check_type_raw:
        raise ValueError("check_type is required")

    # Strip arrow notation (e.g., "Line Debit Amount → Account Code" -> "Line Debit Amount")
    # The arrow indicates which additional fields need to be filled
    check_type = check_type_raw.split(" → ")[0].strip()

    # Get expected value (not required for balance checks)
    expected_value = input.verifier.verifier_values.get("expected_value")

    # Get tolerance (default 0.01)
    try:
        tolerance_value = input.verifier.verifier_values.get("tolerance", 0.01)
        if tolerance_value is None:
            tolerance_value = 0.01
        tolerance = Decimal(str(tolerance_value))
    except (ValueError, TypeError, InvalidOperation) as e:
        raise ValueError(f"Invalid tolerance value: {e!s}") from e

    # Get check configuration
    check_config = CHECK_TYPES.get(check_type)
    if not check_config:
        raise ValueError(f"Unknown check_type: {check_type}")

    # Extract required parameters
    params = {}
    for param in check_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            raise ValueError(
                f"Required parameter '{param}' is missing for {check_type}"
            )
        params[param] = value

    # Get database connection from snapshot_dbs helper
    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
    if not dbs:
        raise ValueError("No databases found in snapshot")

    # Filter to ADP Payroll database only
    # Expected alias: .apps_data_adp_payroll_data (from /.apps_data/adp_payroll/data.db)
    adp_dbs = [
        (alias, db_info)
        for alias, db_info in dbs.items()
        if "adp_payroll" in alias.lower()
    ]

    if len(adp_dbs) != 1:
        raise ValueError(
            f"Expected exactly 1 ADP Payroll database (alias containing 'adp_payroll'), found {len(adp_dbs)}. Available: {list(dbs.keys())}"
        )

    _alias, db_info = adp_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        raise ValueError("ADP database found but connection is None")

    try:
        cursor = conn.cursor()

        # Handle different check types
        if check_type == "Balance Check":
            # Check header totals
            row = cursor.execute(check_config["header_query"], params).fetchone()
            if row is None:
                raise ValueError(f"Journal entry {params.get('je_id')} not found")

            total_debits = Decimal(str(row[0])) if row[0] else Decimal(0)
            total_credits = Decimal(str(row[1])) if row[1] else Decimal(0)
            variance = abs(total_debits - total_credits)
            balanced = variance <= tolerance

            logger.info(
                f"JE Balance Check: debits=${float(total_debits):,.2f}, "
                f"credits=${float(total_credits):,.2f}, variance=${float(variance):,.2f}"
            )

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=1.0 if balanced else 0.0,
                status=VerifierResultStatus.OK,
                verifier_result_values={
                    "check_type": check_type,
                    "je_id": params.get("je_id"),
                    "total_debits": float(total_debits),
                    "total_credits": float(total_credits),
                    "variance": float(variance),
                    "balanced": balanced,
                },
                message=f"Balance Check: debits=${float(total_debits):,.2f}, "
                f"credits=${float(total_credits):,.2f} "
                f"{'✓ Balanced' if balanced else f'✗ Variance: ${float(variance):,.2f}'}",
            )

        elif check_type == "Lines Balance Check":
            # First check if the journal entry has any lines
            # (COALESCE(SUM(...), 0) always returns a row, so we need explicit existence check)
            line_count = cursor.execute(
                "SELECT COUNT(*) FROM journal_entry_lines WHERE je_id = :je_id",
                params,
            ).fetchone()
            if line_count is None or line_count[0] == 0:
                raise ValueError(
                    f"No lines found for journal entry {params.get('je_id')}"
                )

            # Now check sum of lines
            row = cursor.execute(check_config["lines_query"], params).fetchone()

            total_debits = Decimal(str(row[0])) if row[0] else Decimal(0)
            total_credits = Decimal(str(row[1])) if row[1] else Decimal(0)
            variance = abs(total_debits - total_credits)
            balanced = variance <= tolerance

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=1.0 if balanced else 0.0,
                status=VerifierResultStatus.OK,
                verifier_result_values={
                    "check_type": check_type,
                    "je_id": params.get("je_id"),
                    "total_debits": float(total_debits),
                    "total_credits": float(total_credits),
                    "variance": float(variance),
                    "balanced": balanced,
                },
                message=f"Lines Balance Check: Σdebits=${float(total_debits):,.2f}, "
                f"Σcredits=${float(total_credits):,.2f} "
                f"{'✓ Balanced' if balanced else f'✗ Variance: ${float(variance):,.2f}'}",
            )

        else:
            # Standard query-based checks
            if expected_value is None:
                raise ValueError(f"expected_value is required for {check_type}")

            row = cursor.execute(check_config["query"], params).fetchone()

            if row is None:
                return VerifierResult(
                    verifier_id=input.verifier.verifier_id,
                    verifier_version=input.verifier.verifier_version,
                    score=0.0,
                    status=VerifierResultStatus.OK,
                    verifier_result_values={
                        "check_type": check_type,
                        "actual_value": None,
                        "expected_value": expected_value,
                        "matches": False,
                    },
                    message=f"{check_type}: No result found (expected {expected_value})",
                )

            result = row[0]
            result_type = check_config.get("result_type", "decimal")

            if result_type == "boolean":
                actual_bool = result > 0 if result else False
                expected_bool = str(expected_value).lower() in ["true", "1", "yes"]
                matches = actual_bool == expected_bool
                message = f"{check_type}: {'Exists' if actual_bool else 'Not found'} {'✓' if matches else '✗'}"
            elif result_type == "integer":
                try:
                    matches = int(result) == int(expected_value)
                except (ValueError, TypeError) as e:
                    raise ValueError(f"Invalid integer value: {e!s}") from e
                message = f"{check_type}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
            else:
                # Decimal comparison
                try:
                    result_decimal = Decimal(str(result)) if result else Decimal(0)
                    expected_decimal = Decimal(str(expected_value))
                except (ValueError, TypeError, InvalidOperation) as e:
                    raise ValueError(f"Invalid decimal value: {e!s}") from e
                matches = abs(result_decimal - expected_decimal) <= tolerance
                message = f"{check_type}: ${float(result_decimal):,.2f} {'✓' if matches else f'✗ expected ${float(expected_decimal):,.2f}'}"

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=1.0 if matches else 0.0,
                status=VerifierResultStatus.OK,
                verifier_result_values={
                    "check_type": check_type,
                    "actual_value": str(result) if result is not None else None,
                    "expected_value": str(expected_value),
                    "matches": matches,
                },
                message=message,
            )

    except Exception as e:
        logger.error(f"Error executing journal entry check {check_type}: {e}")
        raise ValueError(f"Check failed: {e!s}") from e
