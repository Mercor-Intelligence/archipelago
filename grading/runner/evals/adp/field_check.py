"""ADP Field Check Verifier - Flexible dropdown + value interface for ADP Payroll.

This verifier provides a simple dropdown interface for domain experts to check
any ADP Payroll field without writing SQL. The dropdown selection maps to
pre-defined SQL queries verified against the actual ADP SQLAlchemy schema.

Schema Reference (from mcp_servers/adp_payroll/db/models/):
- payroll_runs: id, pay_group_id, period, status, employee_count, pay_date, posting_date
- payroll_aggregates: id, run_id, gross_wages, regular_pay, overtime_pay, net_pay, is_validated
- tax_aggregates: id, aggregate_id, tax_type, jurisdiction, employee_amount, employer_amount
- benefit_aggregates: id, aggregate_id, benefit_type, employee_amount, employer_amount
- deduction_aggregates: id, aggregate_id, deduction_type, amount
- journal_entries: id, run_id, posting_date, status, total_debits, total_credits
- journal_entry_lines: id, je_id, line_number, account_code, account_name, debit_amount, credit_amount, cost_center, department, description
- reconciliations: id, run_id, status, total_variance
- reconciliation_checks: id, reconciliation_id, check_type, passed, expected_value, actual_value
- employees: id, pay_group_id, is_active
- pay_groups: id, name, frequency, is_active
- forms_941: id, quarter, pay_group_id, total_wages, federal_withholding, etc.
- forms_940: id, year, pay_group_id, total_payments, futa_tax, etc.
- timesheets: id, employee_id, date, hours, job_code
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# Field mappings for ADP Payroll database queries
# All queries verified against actual SQLAlchemy schema
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # =========================================================================
    # Payroll Run Checks
    # =========================================================================
    "Payroll Run Count": {
        "query": "SELECT COUNT(*) FROM payroll_runs",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of payroll runs",
    },
    "Payroll Run Count by Status": {
        "query": "SELECT COUNT(*) FROM payroll_runs WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count payroll runs by status (draft, pending_approval, approved, processing, completed, posted, archived, cancelled)",
    },
    "Payroll Run Count by Pay Group": {
        "query": "SELECT COUNT(*) FROM payroll_runs WHERE pay_group_id = :pay_group_id",
        "required_params": ["pay_group_id"],
        "result_type": "integer",
        "description": "Count payroll runs for a specific pay group",
    },
    "Payroll Run Exists": {
        "query": "SELECT COUNT(*) FROM payroll_runs WHERE id = :run_id",
        "required_params": ["run_id"],
        "result_type": "boolean",
        "description": "Check if a specific payroll run exists",
    },
    "Payroll Run Status": {
        "query": "SELECT status FROM payroll_runs WHERE id = :run_id",
        "required_params": ["run_id"],
        "result_type": "string",
        "description": "Get status of a specific payroll run",
    },
    "Payroll Run Employee Count": {
        "query": "SELECT employee_count FROM payroll_runs WHERE id = :run_id",
        "required_params": ["run_id"],
        "result_type": "integer",
        "description": "Get employee count for a specific payroll run",
    },
    "Payroll Run by Period": {
        "query": "SELECT COUNT(*) FROM payroll_runs WHERE period = :period",
        "required_params": ["period"],
        "result_type": "boolean",
        "description": "Check if a payroll run exists for a specific period (e.g., 2025-01-BW2)",
    },
    "Payroll Run by Period and Pay Group": {
        "query": "SELECT COUNT(*) FROM payroll_runs WHERE period = :period AND pay_group_id = :pay_group_id",
        "required_params": ["period", "pay_group_id"],
        "result_type": "boolean",
        "description": "Check if a payroll run exists for a specific period and pay group",
    },
    # =========================================================================
    # Pay Group Checks
    # =========================================================================
    "Pay Group Count": {
        "query": "SELECT COUNT(*) FROM pay_groups",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of pay groups",
    },
    "Pay Group Count Active": {
        "query": "SELECT COUNT(*) FROM pay_groups WHERE is_active = 1",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active pay groups",
    },
    "Pay Group Exists": {
        "query": "SELECT COUNT(*) FROM pay_groups WHERE id = :pay_group_id",
        "required_params": ["pay_group_id"],
        "result_type": "boolean",
        "description": "Check if a specific pay group exists",
    },
    "Pay Group Frequency": {
        "query": "SELECT frequency FROM pay_groups WHERE id = :pay_group_id",
        "required_params": ["pay_group_id"],
        "result_type": "string",
        "description": "Get frequency for a pay group (W, BW, SM, M)",
    },
    "Pay Group Name": {
        "query": "SELECT name FROM pay_groups WHERE id = :pay_group_id",
        "required_params": ["pay_group_id"],
        "result_type": "string",
        "description": "Get name of a pay group",
    },
    # =========================================================================
    # Employee Checks
    # =========================================================================
    "Employee Count": {
        "query": "SELECT COUNT(*) FROM employees",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of employees",
    },
    "Employee Count Active": {
        "query": "SELECT COUNT(*) FROM employees WHERE is_active = 1",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active employees",
    },
    "Employee Count by Pay Group": {
        "query": "SELECT COUNT(*) FROM employees WHERE pay_group_id = :pay_group_id",
        "required_params": ["pay_group_id"],
        "result_type": "integer",
        "description": "Count employees in a specific pay group",
    },
    "Employee Count Active by Pay Group": {
        "query": "SELECT COUNT(*) FROM employees WHERE pay_group_id = :pay_group_id AND is_active = 1",
        "required_params": ["pay_group_id"],
        "result_type": "integer",
        "description": "Count active employees in a specific pay group",
    },
    "Employee Exists": {
        "query": "SELECT COUNT(*) FROM employees WHERE id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "boolean",
        "description": "Check if a specific employee exists",
    },
    # =========================================================================
    # Payroll Aggregate Checks (payroll_aggregates table)
    # =========================================================================
    "Gross Wages by Run": {
        "query": "SELECT gross_wages FROM payroll_aggregates WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Get gross wages for a specific payroll run",
    },
    "Net Pay by Run": {
        "query": "SELECT net_pay FROM payroll_aggregates WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Get net pay for a specific payroll run",
    },
    "Regular Pay by Run": {
        "query": "SELECT regular_pay FROM payroll_aggregates WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Get regular pay for a specific payroll run",
    },
    "Overtime Pay by Run": {
        "query": "SELECT overtime_pay FROM payroll_aggregates WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Get overtime pay for a specific payroll run",
    },
    "Bonus Pay by Run": {
        "query": "SELECT bonus_pay FROM payroll_aggregates WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Get bonus pay for a specific payroll run",
    },
    "Aggregate Is Validated": {
        "query": "SELECT is_validated FROM payroll_aggregates WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "boolean",
        "description": "Check if gross-to-net has been validated for a payroll run",
    },
    "Aggregate Variance Amount": {
        "query": "SELECT COALESCE(variance_amount, 0) FROM payroll_aggregates WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Get variance amount from validation",
    },
    "Total Gross Wages All Runs": {
        "query": "SELECT COALESCE(SUM(gross_wages), 0) FROM payroll_aggregates",
        "required_params": [],
        "result_type": "decimal",
        "description": "Sum of all gross wages across all payroll runs",
    },
    "Total Net Pay All Runs": {
        "query": "SELECT COALESCE(SUM(net_pay), 0) FROM payroll_aggregates",
        "required_params": [],
        "result_type": "decimal",
        "description": "Sum of all net pay across all payroll runs",
    },
    # =========================================================================
    # Tax Aggregate Checks (tax_aggregates table)
    # Note: FK is aggregate_id -> payroll_aggregates.id, tax_type column
    # =========================================================================
    "Total Employee Taxes by Run": {
        "query": """
            SELECT COALESCE(SUM(ta.employee_amount), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Sum all employee taxes for a payroll run",
    },
    "Total Employer Taxes by Run": {
        "query": """
            SELECT COALESCE(SUM(ta.employer_amount), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Sum all employer taxes for a payroll run",
    },
    "Employee Tax by Type": {
        "query": """
            SELECT COALESCE(SUM(ta.employee_amount), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id AND ta.tax_type = :tax_type
        """,
        "required_params": ["run_id", "tax_type"],
        "result_type": "decimal",
        "description": "Sum employee tax amount for a specific type (federal, state, fica, futa, suta)",
    },
    "Employer Tax by Type": {
        "query": """
            SELECT COALESCE(SUM(ta.employer_amount), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id AND ta.tax_type = :tax_type
        """,
        "required_params": ["run_id", "tax_type"],
        "result_type": "decimal",
        "description": "Sum employer tax amount for a specific type",
    },
    "Tax by Type and Jurisdiction": {
        "query": """
            SELECT COALESCE(SUM(COALESCE(ta.employee_amount, 0) + COALESCE(ta.employer_amount, 0)), 0)
            FROM tax_aggregates ta
            JOIN payroll_aggregates pa ON ta.aggregate_id = pa.id
            WHERE pa.run_id = :run_id AND ta.tax_type = :tax_type AND ta.jurisdiction = :jurisdiction
        """,
        "required_params": ["run_id", "tax_type", "jurisdiction"],
        "result_type": "decimal",
        "description": "Get total tax for a specific type and jurisdiction (e.g., state tax for CA)",
    },
    # =========================================================================
    # Benefit Aggregate Checks (benefit_aggregates table)
    # =========================================================================
    "Total Employee Benefits by Run": {
        "query": """
            SELECT COALESCE(SUM(ba.employee_amount), 0)
            FROM benefit_aggregates ba
            JOIN payroll_aggregates pa ON ba.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Sum all employee benefit deductions for a payroll run",
    },
    "Total Employer Benefits by Run": {
        "query": """
            SELECT COALESCE(SUM(ba.employer_amount), 0)
            FROM benefit_aggregates ba
            JOIN payroll_aggregates pa ON ba.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Sum all employer benefit contributions for a payroll run",
    },
    "Benefit by Type": {
        "query": """
            SELECT COALESCE(SUM(COALESCE(ba.employee_amount, 0) + COALESCE(ba.employer_amount, 0)), 0)
            FROM benefit_aggregates ba
            JOIN payroll_aggregates pa ON ba.aggregate_id = pa.id
            WHERE pa.run_id = :run_id AND ba.benefit_type = :benefit_type
        """,
        "required_params": ["run_id", "benefit_type"],
        "result_type": "decimal",
        "description": "Get total benefit cost for a specific type (health, dental, vision, 401k)",
    },
    # =========================================================================
    # Deduction Aggregate Checks (deduction_aggregates table)
    # =========================================================================
    "Total Other Deductions by Run": {
        "query": """
            SELECT COALESCE(SUM(da.amount), 0)
            FROM deduction_aggregates da
            JOIN payroll_aggregates pa ON da.aggregate_id = pa.id
            WHERE pa.run_id = :run_id
        """,
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Sum all other deductions (garnishments, union dues, etc.) for a payroll run",
    },
    "Deduction by Type": {
        "query": """
            SELECT COALESCE(SUM(da.amount), 0)
            FROM deduction_aggregates da
            JOIN payroll_aggregates pa ON da.aggregate_id = pa.id
            WHERE pa.run_id = :run_id AND da.deduction_type = :deduction_type
        """,
        "required_params": ["run_id", "deduction_type"],
        "result_type": "decimal",
        "description": "Sum deduction amount for a specific type (garnishment, union_dues, etc.)",
    },
    # =========================================================================
    # Journal Entry Checks (journal_entries, journal_entry_lines tables)
    # Note: FK is je_id, columns are debit_amount/credit_amount
    # =========================================================================
    "Journal Entry Count": {
        "query": "SELECT COUNT(*) FROM journal_entries",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total journal entries",
    },
    "Journal Entry Count by Run": {
        "query": "SELECT COUNT(*) FROM journal_entries WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "integer",
        "description": "Count journal entries for a specific payroll run",
    },
    "Journal Entry Exists": {
        "query": "SELECT COUNT(*) FROM journal_entries WHERE id = :je_id",
        "required_params": ["je_id"],
        "result_type": "boolean",
        "description": "Check if a specific journal entry exists",
    },
    "Journal Entry Status": {
        "query": "SELECT status FROM journal_entries WHERE id = :je_id",
        "required_params": ["je_id"],
        "result_type": "string",
        "description": "Get status of a journal entry (draft, posted, reversed)",
    },
    "Journal Entry Total Debits": {
        "query": "SELECT total_debits FROM journal_entries WHERE id = :je_id",
        "required_params": ["je_id"],
        "result_type": "decimal",
        "description": "Get total debits for a journal entry (from header)",
    },
    "Journal Entry Total Credits": {
        "query": "SELECT total_credits FROM journal_entries WHERE id = :je_id",
        "required_params": ["je_id"],
        "result_type": "decimal",
        "description": "Get total credits for a journal entry (from header)",
    },
    "Journal Entry Line Count": {
        "query": "SELECT COUNT(*) FROM journal_entry_lines WHERE je_id = :je_id",
        "required_params": ["je_id"],
        "result_type": "integer",
        "description": "Count lines in a journal entry",
    },
    "Journal Entry Sum Debits from Lines": {
        "query": "SELECT COALESCE(SUM(debit_amount), 0) FROM journal_entry_lines WHERE je_id = :je_id",
        "required_params": ["je_id"],
        "result_type": "decimal",
        "description": "Sum all debit_amount from journal entry lines",
    },
    "Journal Entry Sum Credits from Lines": {
        "query": "SELECT COALESCE(SUM(credit_amount), 0) FROM journal_entry_lines WHERE je_id = :je_id",
        "required_params": ["je_id"],
        "result_type": "decimal",
        "description": "Sum all credit_amount from journal entry lines",
    },
    # =========================================================================
    # Reconciliation Checks (reconciliations, reconciliation_checks tables)
    # Note: checks use 'passed' boolean column
    # =========================================================================
    "Reconciliation Count": {
        "query": "SELECT COUNT(*) FROM reconciliations",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total reconciliations",
    },
    "Reconciliation Exists for Run": {
        "query": "SELECT COUNT(*) FROM reconciliations WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "boolean",
        "description": "Check if reconciliation exists for a payroll run",
    },
    "Reconciliation Status": {
        "query": "SELECT status FROM reconciliations WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "string",
        "description": "Get reconciliation status for a run (clean, variances_noted, requires_review)",
    },
    "Reconciliation Total Variance": {
        "query": "SELECT COALESCE(total_variance, 0) FROM reconciliations WHERE run_id = :run_id",
        "required_params": ["run_id"],
        "result_type": "decimal",
        "description": "Get total variance amount from reconciliation",
    },
    "Reconciliation Check Count": {
        "query": """
            SELECT COUNT(*)
            FROM reconciliation_checks rc
            JOIN reconciliations r ON rc.reconciliation_id = r.id
            WHERE r.run_id = :run_id
        """,
        "required_params": ["run_id"],
        "result_type": "integer",
        "description": "Count reconciliation checks for a run",
    },
    "Reconciliation Passed Count": {
        "query": """
            SELECT COUNT(*)
            FROM reconciliation_checks rc
            JOIN reconciliations r ON rc.reconciliation_id = r.id
            WHERE r.run_id = :run_id AND rc.passed = 1
        """,
        "required_params": ["run_id"],
        "result_type": "integer",
        "description": "Count passed reconciliation checks",
    },
    "Reconciliation Failed Count": {
        "query": """
            SELECT COUNT(*)
            FROM reconciliation_checks rc
            JOIN reconciliations r ON rc.reconciliation_id = r.id
            WHERE r.run_id = :run_id AND rc.passed = 0
        """,
        "required_params": ["run_id"],
        "result_type": "integer",
        "description": "Count failed reconciliation checks",
    },
    # =========================================================================
    # Tax Form Checks (forms_941, forms_940 tables - separate tables!)
    # =========================================================================
    "Form 941 Count": {
        "query": "SELECT COUNT(*) FROM forms_941",
        "required_params": [],
        "result_type": "integer",
        "description": "Count 941 quarterly forms generated",
    },
    "Form 941 Exists for Quarter": {
        "query": "SELECT COUNT(*) FROM forms_941 WHERE quarter = :quarter AND pay_group_id = :pay_group_id",
        "required_params": ["quarter", "pay_group_id"],
        "result_type": "boolean",
        "description": "Check if Form 941 exists for a quarter and pay group",
    },
    "Form 941 Total Wages": {
        "query": "SELECT total_wages FROM forms_941 WHERE quarter = :quarter AND pay_group_id = :pay_group_id",
        "required_params": ["quarter", "pay_group_id"],
        "result_type": "decimal",
        "description": "Get total wages from Form 941",
    },
    "Form 941 Tax Liability": {
        "query": "SELECT total_tax_liability FROM forms_941 WHERE quarter = :quarter AND pay_group_id = :pay_group_id",
        "required_params": ["quarter", "pay_group_id"],
        "result_type": "decimal",
        "description": "Get total tax liability from Form 941",
    },
    "Form 941 Balance Due": {
        "query": "SELECT balance_due FROM forms_941 WHERE quarter = :quarter AND pay_group_id = :pay_group_id",
        "required_params": ["quarter", "pay_group_id"],
        "result_type": "decimal",
        "description": "Get balance due from Form 941",
    },
    "Form 940 Count": {
        "query": "SELECT COUNT(*) FROM forms_940",
        "required_params": [],
        "result_type": "integer",
        "description": "Count 940 annual forms generated",
    },
    "Form 940 Exists for Year": {
        "query": "SELECT COUNT(*) FROM forms_940 WHERE year = :year AND pay_group_id = :pay_group_id",
        "required_params": ["year", "pay_group_id"],
        "result_type": "boolean",
        "description": "Check if Form 940 exists for a year and pay group",
    },
    "Form 940 FUTA Tax": {
        "query": "SELECT futa_tax FROM forms_940 WHERE year = :year AND pay_group_id = :pay_group_id",
        "required_params": ["year", "pay_group_id"],
        "result_type": "decimal",
        "description": "Get FUTA tax from Form 940",
    },
    # =========================================================================
    # Timesheet Checks (timesheets table - uses 'date' not 'period')
    # =========================================================================
    "Timesheet Count": {
        "query": "SELECT COUNT(*) FROM timesheets",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total timesheets",
    },
    "Timesheet Count by Employee": {
        "query": "SELECT COUNT(*) FROM timesheets WHERE employee_id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "integer",
        "description": "Count timesheets for a specific employee",
    },
    "Timesheet Count by Date Range": {
        "query": "SELECT COUNT(*) FROM timesheets WHERE date >= :start_date AND date <= :end_date",
        "required_params": ["start_date", "end_date"],
        "result_type": "integer",
        "description": "Count timesheets in a date range",
    },
    "Timesheet Total Hours by Employee": {
        "query": "SELECT COALESCE(SUM(hours), 0) FROM timesheets WHERE employee_id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "decimal",
        "description": "Sum total hours for an employee",
    },
    "Timesheet Total Hours by Date Range": {
        "query": "SELECT COALESCE(SUM(hours), 0) FROM timesheets WHERE date >= :start_date AND date <= :end_date",
        "required_params": ["start_date", "end_date"],
        "result_type": "decimal",
        "description": "Sum total hours in a date range",
    },
}


async def adp_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any ADP Payroll field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    # Validate helper results available
    if not input.helper_results:
        raise ValueError("Helper results not available")

    # Get field selection
    field_to_check_raw = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check_raw:
        raise ValueError("field_to_check is required")

    # Strip arrow notation (e.g., "Payroll Run Status → Run ID" -> "Payroll Run Status")
    # The arrow indicates which fields need to be filled, but the key is just the base name
    field_to_check = field_to_check_raw.split(" → ")[0].strip()

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        raise ValueError("expected_value is required")

    # Get field configuration
    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        raise ValueError(f"Unknown field_to_check: {field_to_check}")

    # Extract required parameters from verifier_values
    params = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            raise ValueError(
                f"Required parameter '{param}' is missing for {field_to_check}"
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

    # Execute query
    try:
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"ADP field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        raise ValueError(f"Query execution failed: {e!s}") from e

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
            raise ValueError(f"Invalid tolerance value: {e!s}") from e

        try:
            result_decimal = Decimal(str(result))
            expected_decimal = Decimal(str(expected_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            raise ValueError(
                f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {e!s}"
            ) from e
        matches = abs(result_decimal - expected_decimal) <= tolerance
        message = f"{field_to_check}: ${float(result_decimal):,.2f} {'✓' if matches else f'✗ expected ${float(expected_decimal):,.2f}'}"
    elif result_type == "boolean":
        # Convert COUNT results to boolean
        actual_bool = result > 0
        expected_bool = str(expected_value).lower() in ["true", "1", "yes"]
        matches = actual_bool == expected_bool
        # Use appropriate text based on the type of boolean check
        if "Validated" in field_to_check:
            bool_text = "Validated" if actual_bool else "Not validated"
        elif "Exists" in field_to_check or "by Period" in field_to_check:
            bool_text = "Exists" if actual_bool else "Does not exist"
        else:
            bool_text = "Yes" if actual_bool else "No"
        message = f"{field_to_check}: {bool_text} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            matches = int(result) == int(expected_value)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {e!s}"
            ) from e
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    elif result_type == "string":
        matches = str(result).lower() == str(expected_value).lower()
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "actual_value": str(result) if result is not None else None,
            "expected_value": str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
        },
        message=message,
    )
