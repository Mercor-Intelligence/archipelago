"""Xero Field Check Verifier - Check field values in Xero entities."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .utils import (
    calculate_account_balance,
    filter_by_account_class,
    filter_by_field,
    filter_overdue_invoices,
    find_by_code,
    find_by_field,
    find_by_id,
    find_by_name,
    get_nested_field,
    parse_date,
    sum_field,
)

# Field check configurations: data_source, operation, required_params
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # ==========================================================================
    # COUNT OPERATIONS
    # ==========================================================================
    "Bank Transaction Count": {
        "data_source": "bank_transactions",
        "operation": "count",
    },
    "Invoice Count": {
        "data_source": "invoices",
        "operation": "count",
    },
    "Account Count": {
        "data_source": "accounts",
        "operation": "count",
    },
    "Contact Count": {
        "data_source": "contacts",
        "operation": "count",
    },
    "Payment Count": {
        "data_source": "payments",
        "operation": "count",
    },
    # --------------------------------------------------------------------------
    # Count with filters
    # --------------------------------------------------------------------------
    "Invoice Count by Status": {
        "data_source": "invoices",
        "operation": "count_filtered",
        "filter_field": "Status",
        "required_params": ["filter_value"],
    },
    "Account Count by Class": {
        "data_source": "accounts",
        "operation": "count_filtered",
        "filter_field": "Class",
        "required_params": ["filter_value"],
    },
    "Account Count by Type": {
        "data_source": "accounts",
        "operation": "count_filtered",
        "filter_field": "Type",
        "required_params": ["filter_value"],
    },
    "Contact Count by Customer": {
        "data_source": "contacts",
        "operation": "count_filtered",
        "filter_field": "IsCustomer",
        "required_params": ["filter_value"],
    },
    "Overdue Invoice Count": {
        "data_source": "invoices",
        "operation": "count_overdue",
        "required_params": ["reference_date"],
    },
    # ==========================================================================
    # EXISTENCE CHECKS
    # ==========================================================================
    "Bank Transaction Exists": {
        "data_source": "bank_transactions",
        "operation": "exists",
        "id_field": "BankTransactionID",
        "required_params": ["transaction_id"],
    },
    "Invoice Exists": {
        "data_source": "invoices",
        "operation": "exists",
        "id_field": "InvoiceID",
        "required_params": ["invoice_id"],
    },
    "Invoice Exists by Number": {
        "data_source": "invoices",
        "operation": "exists_by_field",
        "field": "InvoiceNumber",
        "required_params": ["invoice_number"],
    },
    "Account Exists": {
        "data_source": "accounts",
        "operation": "exists_by_code",
        "required_params": ["account_code"],
    },
    "Account Exists by Class": {
        "data_source": "accounts",
        "operation": "exists_by_field",
        "field": "Class",
        "required_params": ["account_class"],
    },
    "Contact Exists": {
        "data_source": "contacts",
        "operation": "exists",
        "id_field": "ContactID",
        "required_params": ["contact_id"],
    },
    "Contact Exists by Name": {
        "data_source": "contacts",
        "operation": "exists_by_name",
        "required_params": ["contact_name"],
    },
    # ==========================================================================
    # FIELD VALUE CHECKS
    # ==========================================================================
    "Bank Transaction Field": {
        "data_source": "bank_transactions",
        "operation": "get_field",
        "id_field": "BankTransactionID",
        "required_params": ["transaction_id", "field_name"],
    },
    "Bank Transaction Account Code": {
        "data_source": "bank_transactions",
        "operation": "get_line_item_account",
        "id_field": "BankTransactionID",
        "required_params": ["transaction_id"],
    },
    "Invoice Field": {
        "data_source": "invoices",
        "operation": "get_field",
        "id_field": "InvoiceID",
        "required_params": ["invoice_id", "field_name"],
    },
    "Invoice Field by Number": {
        "data_source": "invoices",
        "operation": "get_field_by_invoice_number",
        "required_params": ["invoice_number", "field_name"],
    },
    "Account Field": {
        "data_source": "accounts",
        "operation": "get_field_by_code",
        "required_params": ["account_code", "field_name"],
    },
    "Account Field by Class": {
        "data_source": "accounts",
        "operation": "get_field_by_class",
        "required_params": ["account_class", "field_name"],
    },
    "Account Balance": {
        "data_source": "accounts",
        "operation": "get_account_balance",
        "required_params": ["account_code"],
    },
    "Contact Field": {
        "data_source": "contacts",
        "operation": "get_field",
        "id_field": "ContactID",
        "required_params": ["contact_id", "field_name"],
    },
    "Contact Field by Name": {
        "data_source": "contacts",
        "operation": "get_field_by_name",
        "required_params": ["contact_name", "field_name"],
    },
    # Sum operations
    "Sum Bank Transaction Totals": {
        "data_source": "bank_transactions",
        "operation": "sum",
        "field": "Total",
    },
    "Sum Invoice Totals": {
        "data_source": "invoices",
        "operation": "sum",
        "field": "Total",
    },
    # ==========================================================================
    # INVOICE-SPECIFIC CHECKS
    # ==========================================================================
    "Invoice Due Date": {
        "data_source": "invoices",
        "operation": "get_field_by_invoice_number",
        "required_params": ["invoice_number"],
        "fixed_field": "DueDate",
        "description": "Get due date of a specific invoice by InvoiceNumber",
    },
    "Invoice Payment Status": {
        "data_source": "invoices",
        "operation": "get_invoice_payment_status",
        "required_params": ["invoice_number"],
        "description": "Check payment status: PAID, UNPAID, or PARTIAL",
    },
    "Unpaid Invoice Count": {
        "data_source": "invoices",
        "operation": "count_unpaid",
        "description": "Count invoices with AmountDue > 0",
    },
    "Partial Payment Invoice Count": {
        "data_source": "invoices",
        "operation": "count_partial_payment",
        "description": "Count invoices with 0 < AmountDue < Total",
    },
    # ==========================================================================
    # REVENUE CALCULATIONS
    # ==========================================================================
    "Total Revenue": {
        "data_source": "invoices",
        "operation": "sum",
        "field": "Total",
        "description": "Sum all invoice totals (Total field)",
    },
    "Revenue by Account Code": {
        "data_source": "invoices",
        "operation": "sum_by_account_code",
        "required_params": ["account_code"],
        "description": "Sum invoice line item amounts for a specific account code",
    },
}


def _execute_operation(
    data: list[dict[str, Any]],
    field_config: dict[str, Any],
    verifier_values: dict[str, Any],
    xero_state: dict[str, Any] | None = None,
) -> Any:
    """Execute the configured operation on the data.

    Args:
        data: The primary data source (accounts, invoices, etc.)
        field_config: Configuration for the field check
        verifier_values: Values from the verifier configuration
        xero_state: Full Xero state for operations that need cross-entity data
    """
    operation = field_config["operation"]

    # ==========================================================================
    # COUNT OPERATIONS
    # ==========================================================================
    if operation == "count":
        return len(data)

    elif operation == "count_filtered":
        filter_field = field_config.get("filter_field", "Status")
        filter_value = str(verifier_values.get("filter_value") or "")
        filtered = filter_by_field(data, filter_field, filter_value)
        return len(filtered)

    elif operation == "count_overdue":
        reference_date_str = str(verifier_values.get("reference_date") or "")
        reference_date = parse_date(reference_date_str)
        if not reference_date:
            reference_date = datetime.now(UTC)
        overdue = filter_overdue_invoices(data, reference_date)
        return len(overdue)

    # ==========================================================================
    # EXISTENCE CHECKS
    # ==========================================================================
    elif operation == "exists":
        id_field = field_config["id_field"]
        id_param = field_config["required_params"][0]
        id_value = str(verifier_values.get(id_param) or "")
        return find_by_id(data, id_field, id_value) is not None

    elif operation == "exists_by_code":
        code = str(verifier_values.get("account_code") or "")
        return find_by_code(data, code) is not None

    elif operation == "exists_by_name":
        name = str(verifier_values.get("contact_name") or "")
        return find_by_name(data, name) is not None

    elif operation == "exists_by_field":
        field = field_config.get("field", "")
        param_name = field_config["required_params"][0]
        value = str(verifier_values.get(param_name) or "")
        return find_by_field(data, field, value) is not None

    # ==========================================================================
    # FIELD VALUE RETRIEVAL
    # ==========================================================================
    elif operation == "get_field":
        id_field = field_config["id_field"]
        id_param = field_config["required_params"][0]
        id_value = str(verifier_values.get(id_param) or "")
        field_name = str(verifier_values.get("field_name") or "")
        record = find_by_id(data, id_field, id_value)
        if record:
            return get_nested_field(record, field_name)
        return None

    elif operation == "get_field_by_code":
        code = str(verifier_values.get("account_code") or "")
        field_name = str(verifier_values.get("field_name") or "")
        record = find_by_code(data, code)
        if record:
            return get_nested_field(record, field_name)
        return None

    elif operation == "get_field_by_class":
        account_class = str(verifier_values.get("account_class") or "")
        field_name = str(verifier_values.get("field_name") or "")
        # Return first account matching class
        filtered = filter_by_account_class(data, account_class)
        if filtered:
            return get_nested_field(filtered[0], field_name)
        return None

    elif operation == "get_field_by_name":
        name = str(verifier_values.get("contact_name") or "")
        field_name = str(verifier_values.get("field_name") or "")
        record = find_by_name(data, name)
        if record:
            return get_nested_field(record, field_name)
        return None

    elif operation == "get_field_by_invoice_number":
        invoice_number = str(verifier_values.get("invoice_number") or "")
        # Check if field is fixed (e.g., DueDate for "Invoice Due Date")
        field_name = field_config.get("fixed_field") or str(
            verifier_values.get("field_name") or ""
        )
        record = find_by_field(data, "InvoiceNumber", invoice_number)
        if record:
            return get_nested_field(record, field_name)
        return None

    elif operation == "get_account_balance":
        code = str(verifier_values.get("account_code") or "")
        if xero_state:
            return calculate_account_balance(
                accounts=xero_state.get("accounts", []),
                invoices=xero_state.get("invoices", []),
                bank_transactions=xero_state.get("bank_transactions", []),
                payments=xero_state.get("payments", []),
                code=code,
            )
        return None

    elif operation == "get_line_item_account":
        id_field = field_config["id_field"]
        id_param = field_config["required_params"][0]
        id_value = str(verifier_values.get(id_param) or "")
        line_index = int(verifier_values.get("line_item_index", 0) or 0)
        record = find_by_id(data, id_field, id_value)
        if record:
            line_items = record.get("LineItems", [])
            if 0 <= line_index < len(line_items):
                return line_items[line_index].get("AccountCode")
        return None

    # ==========================================================================
    # SUM OPERATIONS
    # ==========================================================================
    elif operation == "sum":
        field = field_config.get("field", "Total")
        return sum_field(data, field)

    elif operation == "sum_by_account_code":
        account_code = str(verifier_values.get("account_code") or "")
        total = Decimal("0")
        for invoice in data:
            line_items = invoice.get("LineItems", [])
            for line_item in line_items:
                if str(line_item.get("AccountCode", "")) == account_code:
                    try:
                        amount = Decimal(str(line_item.get("LineAmount", 0)))
                        total += amount
                    except (ValueError, TypeError, InvalidOperation):
                        pass
        return float(total)

    # ==========================================================================
    # INVOICE-SPECIFIC OPERATIONS
    # ==========================================================================
    elif operation == "get_invoice_payment_status":
        invoice_number = str(verifier_values.get("invoice_number") or "")
        record = find_by_field(data, "InvoiceNumber", invoice_number)
        if not record:
            return None

        try:
            total = Decimal(str(record.get("Total", 0)))
            amount_due = Decimal(str(record.get("AmountDue", total)))

            if amount_due <= 0:
                return "PAID"
            elif amount_due >= total:
                return "UNPAID"
            else:
                return "PARTIAL"
        except (ValueError, TypeError, InvalidOperation):
            return None

    elif operation == "count_unpaid":
        count = 0
        for invoice in data:
            try:
                amount_due = Decimal(
                    str(invoice.get("AmountDue", invoice.get("Total", 0)))
                )
                if amount_due > 0:
                    count += 1
            except (ValueError, TypeError, InvalidOperation):
                pass
        return count

    elif operation == "count_partial_payment":
        count = 0
        for invoice in data:
            try:
                total = Decimal(str(invoice.get("Total", 0)))
                amount_due = Decimal(str(invoice.get("AmountDue", total)))
                if 0 < amount_due < total:
                    count += 1
            except (ValueError, TypeError, InvalidOperation):
                pass
        return count

    raise ValueError(f"Unknown operation: {operation}")


def _compare_values(actual: Any, expected: Any, tolerance: Decimal | None) -> bool:
    """Compare actual vs expected with optional tolerance for numbers."""
    if actual is None:
        return False

    # Try numeric comparison
    try:
        actual_num = Decimal(str(actual))
        expected_num = Decimal(str(expected))
        if tolerance:
            return abs(actual_num - expected_num) <= tolerance
        return actual_num == expected_num
    except Exception:
        pass

    # String comparison (case-insensitive)
    if str(actual).lower() == str(expected).lower():
        return True

    # If no exact match, try parsing as dates (handles "/Date(...)/" vs "2025-09-05")
    actual_date = parse_date(str(actual))
    expected_date = parse_date(str(expected))
    if actual_date is not None and expected_date is not None:
        return actual_date.date() == expected_date.date()

    return False


async def xero_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify Xero field values using dropdown + value interface."""
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

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

    # Validate required params
    for param in field_config.get("required_params", []):
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

    data_source = field_config["data_source"]
    data = xero_state.get(data_source, [])

    try:
        result = _execute_operation(
            data, field_config, input.verifier.verifier_values, xero_state
        )
        logger.info(
            f"Xero field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing field check: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Field check failed: {str(e)}",
        )

    # Get tolerance for numeric comparison
    tolerance = None
    tolerance_str = input.verifier.verifier_values.get("tolerance")
    if tolerance_str:
        try:
            tolerance = Decimal(str(tolerance_str))
        except (ValueError, TypeError, InvalidOperation):
            pass

    matches = _compare_values(result, expected_value, tolerance)
    score = 1.0 if matches else 0.0

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "field_to_check": field_to_check,
            "expected_value": expected_value,
            "actual_value": result,
            "matches": matches,
        },
        message=f"Field check {'passed' if matches else 'failed'}: {field_to_check} = {result}",
    )
