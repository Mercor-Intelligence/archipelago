"""Xero Calculation Verifier - Financial calculations on Xero data."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import (
    calculate_cash_balance_with_starting,
    filter_by_type,
    filter_invoices_by_aging_bucket,
    parse_date,
    sum_amount_due,
    sum_field,
    sum_invoices,
    sum_invoices_by_contact,
    sum_transactions_by_account,
)

# Calculation type configurations
CALCULATION_TYPES: dict[str, dict[str, Any]] = {
    # ==========================================================================
    # CASH FLOW CALCULATIONS
    # ==========================================================================
    "Cash Balance": {
        "description": "Bank inflows minus outflows",
        "operation": "cash_balance",
    },
    "Cash Balance with Starting": {
        "description": "Starting balance + inflows - outflows",
        "operation": "cash_balance_with_starting",
        "required_params": ["starting_balance"],
    },
    "Total Inflows": {
        "description": "Sum of RECEIVE transactions",
        "operation": "sum_by_type",
        "tx_type": "RECEIVE",
    },
    "Total Outflows": {
        "description": "Sum of SPEND transactions",
        "operation": "sum_by_type",
        "tx_type": "SPEND",
    },
    # Account-specific calculations
    "Revenue by Account": {
        "description": "Sum transactions for a revenue account",
        "operation": "sum_by_account",
        "required_params": ["account_code"],
    },
    "Expense by Account": {
        "description": "Sum transactions for an expense account",
        "operation": "sum_by_account",
        "required_params": ["account_code"],
    },
    "Transactions by Account": {
        "description": "Sum transactions for any account code",
        "operation": "sum_by_account",
        "required_params": ["account_code"],
    },
    # P&L calculations
    "Total Revenue": {
        "description": "Sum of invoice totals (ACCREC type)",
        "operation": "total_revenue",
    },
    "Total Expenses": {
        "description": "Sum of SPEND transactions",
        "operation": "total_expenses",
    },
    "Net Income": {
        "description": "Total revenue minus total expenses",
        "operation": "net_income",
    },
    # Balance sheet calculations
    "Total Liabilities": {
        "description": "Sum of liability account transactions",
        "operation": "sum_by_account_type",
        "account_codes": ["700", "720"],  # Notes Payable, Accounts Payable
    },
    "Total Equity": {
        "description": "Owner's capital plus net income",
        "operation": "total_equity",
    },
    "Debt to Equity Ratio": {
        "description": "Total liabilities / total equity",
        "operation": "debt_to_equity",
    },
    # ==========================================================================
    # INVOICE CALCULATIONS
    # ==========================================================================
    "Invoice Total": {
        "description": "Sum of all invoice totals",
        "operation": "sum_invoices",
    },
    "Invoice Total by Type": {
        "description": "Sum invoices by type (ACCREC/ACCPAY)",
        "operation": "sum_invoices_by_type",
        "required_params": ["invoice_type"],
    },
    "Invoice Total by Contact": {
        "description": "Sum invoices for a specific contact",
        "operation": "sum_invoices_by_contact",
        "required_params": ["contact_name"],
    },
    "Invoice Total by Contact ID": {
        "description": "Sum invoices for a specific contact ID",
        "operation": "sum_invoices_by_contact_id",
        "required_params": ["contact_id"],
    },
    "Sum Amount Due": {
        "description": "Sum of AmountDue across all invoices",
        "operation": "sum_amount_due",
    },
    # ==========================================================================
    # AR AGING CALCULATIONS
    # ==========================================================================
    "AR Aging Bucket": {
        "description": "Sum invoices in aging bucket (0-30, 31-60, 61-90, 90+)",
        "operation": "ar_aging_bucket",
        "required_params": ["bucket", "reference_date"],
    },
    "AR Aging Allowance": {
        "description": "Calculate allowance for aging bucket (amount × rate)",
        "operation": "ar_aging_allowance",
        "required_params": ["bucket", "provision_rate", "reference_date"],
    },
    "Total AR Allowance": {
        "description": "Total allowance across all aging buckets",
        "operation": "total_ar_allowance",
        "required_params": ["reference_date"],
    },
    # ==========================================================================
    # FINANCIAL RATIOS
    # ==========================================================================
    "Current Ratio": {
        "description": "Current Assets / Current Liabilities",
        "operation": "current_ratio",
    },
    "Quick Ratio": {
        "description": "(Cash + AR) / Current Liabilities",
        "operation": "quick_ratio",
    },
    "Days Sales Outstanding": {
        "description": "(AR / Credit Sales) × Period Days",
        "operation": "dso",
        "required_params": ["credit_sales"],
    },
    "Client Retention Rate": {
        "description": "(Clients with 2+ invoices / Total clients) × 100",
        "operation": "client_retention_rate",
    },
}


def _execute_calculation(
    xero_state: dict[str, Any],
    calc_config: dict[str, Any],
    verifier_values: dict[str, Any],
) -> Decimal:
    """Execute the configured calculation."""
    operation = calc_config["operation"]
    transactions = xero_state.get("bank_transactions", [])
    invoices = xero_state.get("invoices", [])
    _accounts = xero_state.get("accounts", [])

    # ==========================================================================
    # CASH FLOW CALCULATIONS
    # ==========================================================================
    if operation == "cash_balance":
        inflows = sum_field(filter_by_type(transactions, "RECEIVE"), "Total")
        outflows = sum_field(filter_by_type(transactions, "SPEND"), "Total")
        return inflows - outflows

    elif operation == "cash_balance_with_starting":
        starting_value = verifier_values.get("starting_balance", 0)
        if starting_value is None:
            starting_value = 0
        starting = Decimal(str(starting_value))
        return calculate_cash_balance_with_starting(transactions, starting)

    elif operation == "sum_by_type":
        tx_type = calc_config.get(
            "tx_type", verifier_values.get("transaction_type", "RECEIVE")
        )
        filtered = filter_by_type(transactions, tx_type)
        return sum_field(filtered, "Total")

    elif operation == "sum_by_account":
        account_code = str(verifier_values.get("account_code") or "")
        return sum_transactions_by_account(transactions, account_code)

    elif operation == "total_revenue":
        # Revenue from invoices (ACCREC = accounts receivable = sales)
        accrec_invoices = [i for i in invoices if i.get("Type") == "ACCREC"]
        invoice_revenue = sum_invoices(accrec_invoices)
        # Plus other income from bank transactions (account 210, etc.)
        other_income = sum_transactions_by_account(
            filter_by_type(transactions, "RECEIVE"), "210"
        )
        return invoice_revenue + other_income

    elif operation == "total_expenses":
        spend_transactions = filter_by_type(transactions, "SPEND")
        # Exclude liability/equity transactions from expenses
        expense_total = Decimal("0")
        for txn in spend_transactions:
            for item in txn.get("LineItems", []):
                code = str(item.get("AccountCode", ""))
                # Expense accounts typically start with 4, 5, 6
                # Exclude 7xx (liability), 8xx (equity), 1xx (asset)
                if code and code[0] in ("4", "5", "6"):
                    try:
                        expense_total += Decimal(str(item.get("LineAmount", 0)))
                    except (ValueError, TypeError, InvalidOperation):
                        pass
        return expense_total

    elif operation == "net_income":
        # Revenue
        accrec_invoices = [i for i in invoices if i.get("Type") == "ACCREC"]
        revenue = sum_invoices(accrec_invoices)
        other_income = sum_transactions_by_account(
            filter_by_type(transactions, "RECEIVE"), "210"
        )
        total_revenue = revenue + other_income

        # Expenses (from SPEND transactions, expense accounts only)
        spend_transactions = filter_by_type(transactions, "SPEND")
        total_expenses = Decimal("0")
        for txn in spend_transactions:
            for item in txn.get("LineItems", []):
                code = str(item.get("AccountCode", ""))
                if code and code[0] in ("4", "5", "6"):
                    try:
                        total_expenses += Decimal(str(item.get("LineAmount", 0)))
                    except (ValueError, TypeError, InvalidOperation):
                        pass

        return total_revenue - total_expenses

    elif operation == "sum_by_account_type":
        account_codes = calc_config.get("account_codes", [])
        total = Decimal("0")
        for code in account_codes:
            total += sum_transactions_by_account(transactions, code)
        return total

    elif operation == "total_equity":
        # Owner's capital (account 800) from RECEIVE transactions
        owner_capital = sum_transactions_by_account(
            filter_by_type(transactions, "RECEIVE"), "800"
        )
        # Plus net income
        accrec_invoices = [i for i in invoices if i.get("Type") == "ACCREC"]
        revenue = sum_invoices(accrec_invoices)
        other_income = sum_transactions_by_account(
            filter_by_type(transactions, "RECEIVE"), "210"
        )
        total_revenue = revenue + other_income

        spend_transactions = filter_by_type(transactions, "SPEND")
        total_expenses = Decimal("0")
        for txn in spend_transactions:
            for item in txn.get("LineItems", []):
                code = str(item.get("AccountCode", ""))
                if code and code[0] in ("4", "5", "6"):
                    try:
                        total_expenses += Decimal(str(item.get("LineAmount", 0)))
                    except (ValueError, TypeError, InvalidOperation):
                        pass

        net_income = total_revenue - total_expenses
        return owner_capital + net_income

    elif operation == "debt_to_equity":
        # Liabilities (account 700 = Notes Payable)
        liabilities = sum_transactions_by_account(
            filter_by_type(transactions, "RECEIVE"), "700"
        )

        # Equity (same as total_equity calculation)
        owner_capital = sum_transactions_by_account(
            filter_by_type(transactions, "RECEIVE"), "800"
        )
        accrec_invoices = [i for i in invoices if i.get("Type") == "ACCREC"]
        revenue = sum_invoices(accrec_invoices)
        other_income = sum_transactions_by_account(
            filter_by_type(transactions, "RECEIVE"), "210"
        )
        total_revenue = revenue + other_income

        spend_transactions = filter_by_type(transactions, "SPEND")
        total_expenses = Decimal("0")
        for txn in spend_transactions:
            for item in txn.get("LineItems", []):
                code = str(item.get("AccountCode", ""))
                if code and code[0] in ("4", "5", "6"):
                    try:
                        total_expenses += Decimal(str(item.get("LineAmount", 0)))
                    except (ValueError, TypeError, InvalidOperation):
                        pass

        net_income = total_revenue - total_expenses
        equity = owner_capital + net_income

        if equity == 0:
            return Decimal("0")
        return liabilities / equity

    elif operation == "sum_invoices":
        return sum_invoices(invoices)

    elif operation == "sum_invoices_by_type":
        invoice_type = str(verifier_values.get("invoice_type") or "ACCREC")
        filtered = [i for i in invoices if i.get("Type") == invoice_type]
        return sum_invoices(filtered)

    elif operation == "sum_invoices_by_contact":
        contact_name = str(verifier_values.get("contact_name") or "")
        return sum_invoices_by_contact(invoices, contact_name=contact_name)

    elif operation == "sum_invoices_by_contact_id":
        contact_id = str(verifier_values.get("contact_id") or "")
        return sum_invoices_by_contact(invoices, contact_id=contact_id)

    elif operation == "sum_amount_due":
        return sum_amount_due(invoices)

    # ==========================================================================
    # AR AGING CALCULATIONS
    # ==========================================================================
    elif operation == "ar_aging_bucket":
        bucket = str(verifier_values.get("bucket") or "0-30")
        reference_date_str = str(verifier_values.get("reference_date") or "")
        reference_date = parse_date(reference_date_str) or datetime.now(UTC)
        filtered = filter_invoices_by_aging_bucket(invoices, bucket, reference_date)
        return sum_field(filtered, "AmountDue")

    elif operation == "ar_aging_allowance":
        bucket = str(verifier_values.get("bucket") or "0-30")
        rate_value = verifier_values.get("provision_rate", 0)
        if rate_value is None:
            rate_value = 0
        rate = Decimal(str(rate_value))
        reference_date_str = str(verifier_values.get("reference_date") or "")
        reference_date = parse_date(reference_date_str) or datetime.now(UTC)
        filtered = filter_invoices_by_aging_bucket(invoices, bucket, reference_date)
        bucket_total = sum_field(filtered, "AmountDue")
        return bucket_total * rate

    elif operation == "total_ar_allowance":
        # Default provision rates per bucket
        default_rates = {
            "0-30": Decimal("0.02"),
            "31-60": Decimal("0.10"),
            "61-90": Decimal("0.25"),
            "90+": Decimal("0.50"),
        }
        reference_date_str = str(verifier_values.get("reference_date") or "")
        reference_date = parse_date(reference_date_str) or datetime.now(UTC)

        total_allowance = Decimal("0")
        for bucket, rate in default_rates.items():
            filtered = filter_invoices_by_aging_bucket(invoices, bucket, reference_date)
            bucket_total = sum_field(filtered, "AmountDue")
            total_allowance += bucket_total * rate
        return total_allowance

    # ==========================================================================
    # FINANCIAL RATIOS
    # ==========================================================================
    elif operation == "current_ratio":
        bank_balance = Decimal("0")
        for txn in transactions:
            tx_type = (txn.get("Type") or "").upper()
            try:
                total = Decimal(str(txn.get("Total", 0)))
                if tx_type == "RECEIVE":
                    bank_balance += total
                elif tx_type == "SPEND":
                    bank_balance -= total
            except (ValueError, TypeError, InvalidOperation):
                pass

        ar_balance = Decimal("0")
        for inv in invoices:
            if inv.get("Type") == "ACCREC":
                amount_due = inv.get("AmountDue")
                if amount_due is None:
                    amount_due = inv.get("Total", 0)
                try:
                    ar_balance += Decimal(str(amount_due))
                except (ValueError, TypeError, InvalidOperation):
                    pass

        current_assets = bank_balance + ar_balance

        ap_balance = Decimal("0")
        for inv in invoices:
            if inv.get("Type") == "ACCPAY":
                amount_due = inv.get("AmountDue")
                if amount_due is None:
                    amount_due = inv.get("Total", 0)
                try:
                    ap_balance += Decimal(str(amount_due))
                except (ValueError, TypeError, InvalidOperation):
                    pass

        current_liabilities = abs(ap_balance)

        if current_liabilities == 0:
            return Decimal("0")
        return current_assets / current_liabilities

    elif operation == "quick_ratio":
        # Bank balance
        bank_balance = Decimal("0")
        for txn in transactions:
            tx_type = (txn.get("Type") or "").upper()
            try:
                total = Decimal(str(txn.get("Total", 0)))
                if tx_type == "RECEIVE":
                    bank_balance += total
                elif tx_type == "SPEND":
                    bank_balance -= total
            except (ValueError, TypeError, InvalidOperation):
                pass

        # AR balance
        ar_balance = Decimal("0")
        for inv in invoices:
            if inv.get("Type") == "ACCREC":
                amount_due = inv.get("AmountDue")
                if amount_due is None:
                    amount_due = inv.get("Total", 0)
                try:
                    ar_balance += Decimal(str(amount_due))
                except (ValueError, TypeError, InvalidOperation):
                    pass

        quick_assets = bank_balance + ar_balance

        ap_balance = Decimal("0")
        for inv in invoices:
            if inv.get("Type") == "ACCPAY":
                amount_due = inv.get("AmountDue")
                if amount_due is None:
                    amount_due = inv.get("Total", 0)
                try:
                    ap_balance += Decimal(str(amount_due))
                except (ValueError, TypeError, InvalidOperation):
                    pass

        current_liabilities = abs(ap_balance)

        if current_liabilities == 0:
            return Decimal("0")
        return quick_assets / current_liabilities

    elif operation == "dso":
        credit_sales_value = verifier_values.get("credit_sales", 0)
        if credit_sales_value is None:
            credit_sales_value = 0
        credit_sales = Decimal(str(credit_sales_value))
        period_days_value = verifier_values.get("period_days", 365)
        if period_days_value is None:
            period_days_value = 365
        period_days = int(period_days_value)

        # AR = sum of AmountDue
        ar = sum_amount_due(invoices)

        if credit_sales == 0:
            return Decimal("0")
        return (ar / credit_sales) * Decimal(str(period_days))

    elif operation == "client_retention_rate":
        # Count invoices per contact
        contact_invoice_counts: dict[str, int] = {}
        for inv in invoices:
            contact_name = inv.get("Contact", {}).get("Name") or inv.get(
                "Contact.Name", ""
            )
            if contact_name:
                contact_invoice_counts[contact_name] = (
                    contact_invoice_counts.get(contact_name, 0) + 1
                )

        total_clients = len(contact_invoice_counts)
        repeat_clients = sum(
            1 for count in contact_invoice_counts.values() if count >= 2
        )

        if total_clients == 0:
            return Decimal("0")
        return (Decimal(str(repeat_clients)) / Decimal(str(total_clients))) * Decimal(
            "100"
        )

    raise ValueError(f"Unknown calculation: {operation}")


async def xero_calculation_eval(input: EvalImplInput) -> VerifierResult:
    """Perform financial calculations on Xero data."""
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    calculation_type = input.verifier.verifier_values.get("calculation_type")
    if not calculation_type:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="calculation_type is required",
        )

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

    calc_config = CALCULATION_TYPES.get(calculation_type)
    if not calc_config:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Unknown calculation_type: {calculation_type}",
        )

    # Validate required params
    for param in calc_config.get("required_params", []):
        if input.verifier.verifier_values.get(param) is None:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Required parameter '{param}' is missing",
            )

    xero_state = input.helper_results.get(HelperIds.XERO_STATE)
    if not xero_state:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Xero state not available from helper",
        )

    try:
        result = _execute_calculation(
            xero_state, calc_config, input.verifier.verifier_values
        )
        logger.info(
            f"Xero calculation: {calculation_type} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing calculation: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Calculation failed: {str(e)}",
        )

    # Compare with tolerance
    diff = Decimal("0")
    try:
        tolerance_value = input.verifier.verifier_values.get("tolerance", "0.01")
        if tolerance_value is None:
            tolerance_value = "0.01"
        tolerance = Decimal(str(tolerance_value))
        expected_num = Decimal(str(expected_value))
        diff = abs(result - expected_num)
        matches = diff <= tolerance
    except (ValueError, TypeError, InvalidOperation):
        matches = False
        tolerance = Decimal("0.01")  # Default for output

    score = 1.0 if matches else 0.0

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "calculation_type": calculation_type,
            "expected_value": str(expected_value),
            "actual_value": str(result),
            "tolerance": str(tolerance),
            "difference": str(diff),
            "matches": matches,
        },
        message=f"Calculation {'passed' if matches else 'failed'}: {calculation_type} = {result}",
    )
