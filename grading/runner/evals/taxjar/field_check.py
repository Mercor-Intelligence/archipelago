"""TaxJar Field Check Verifier - Flexible dropdown + value interface.

This verifier provides a simple dropdown interface for domain experts to check
any TaxJar field without complex configuration. The dropdown selection maps to
pre-defined checks against the TaxJar state.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .generic import aggregate_field, get_nested_field
from .utils import (
    filter_transactions_by_city,
    filter_transactions_by_date_range,
    filter_transactions_by_exemption_type,
    filter_transactions_by_state,
    filter_transactions_by_zip,
    find_nexus_region,
    find_transaction_by_id,
    get_quarter_date_range,
    get_rate_for_zip,
    get_transaction_field,
    parse_transaction_date,
)

# Field check configurations
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # Order-related checks
    "Order Count": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["date_range", "state"],
        "result_type": "integer",
        "description": "Count total number of orders (optionally filtered by date range and/or state)",
    },
    "Order Count (Q1)": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["quarter_1"],
        "result_type": "integer",
        "description": "Count orders in Q1 (Jan 1 - Mar 31)",
    },
    "Order Count (Q2)": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["quarter_2"],
        "result_type": "integer",
        "description": "Count orders in Q2 (Apr 1 - Jun 30)",
    },
    "Order Count (Q3)": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["quarter_3"],
        "result_type": "integer",
        "description": "Count orders in Q3 (Jul 1 - Sep 30)",
    },
    "Order Count (Q4)": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["quarter_4"],
        "result_type": "integer",
        "description": "Count orders in Q4 (Oct 1 - Dec 31)",
    },
    "Order Exists": {
        "data_source": "orders",
        "operation": "exists",
        "required_params": ["transaction_id"],
        "result_type": "boolean",
        "description": "Check if an order with the given transaction ID exists",
    },
    # Refund-related checks
    "Refund Count": {
        "data_source": "refunds",
        "operation": "count",
        "filters": ["date_range", "state"],
        "result_type": "integer",
        "description": "Count total number of refunds (optionally filtered by date range and/or state)",
    },
    "Refund Count (Q1)": {
        "data_source": "refunds",
        "operation": "count",
        "filters": ["quarter_1"],
        "result_type": "integer",
        "description": "Count refunds in Q1 (Jan 1 - Mar 31)",
    },
    "Refund Count (Q2)": {
        "data_source": "refunds",
        "operation": "count",
        "filters": ["quarter_2"],
        "result_type": "integer",
        "description": "Count refunds in Q2 (Apr 1 - Jun 30)",
    },
    "Refund Count (Q3)": {
        "data_source": "refunds",
        "operation": "count",
        "filters": ["quarter_3"],
        "result_type": "integer",
        "description": "Count refunds in Q3 (Jul 1 - Sep 30)",
    },
    "Refund Count (Q4)": {
        "data_source": "refunds",
        "operation": "count",
        "filters": ["quarter_4"],
        "result_type": "integer",
        "description": "Count refunds in Q4 (Oct 1 - Dec 31)",
    },
    "Refund Exists": {
        "data_source": "refunds",
        "operation": "exists",
        "required_params": ["transaction_id"],
        "result_type": "boolean",
        "description": "Check if a refund with the given transaction ID exists",
    },
    # Nexus-related checks
    "Nexus Region Count": {
        "data_source": "nexus_regions",
        "operation": "count",
        "result_type": "integer",
        "description": "Count total number of nexus regions",
    },
    "Nexus Region Exists": {
        "data_source": "nexus_regions",
        "operation": "region_exists",
        "required_params": ["region_code"],
        "result_type": "boolean",
        "description": "Check if nexus exists in a specific region (state/province)",
    },
    "Nexus Region Code": {
        "data_source": "nexus_regions",
        "operation": "get_region_code",
        "required_params": ["region_index"],
        "result_type": "string",
        "description": "Get the region code at a specific index (0-based)",
    },
    "Nexus Region Codes List": {
        "data_source": "nexus_regions",
        "operation": "list_region_codes",
        "result_type": "string",
        "description": "Get comma-separated list of all nexus region codes",
    },
    # Transaction totals
    "Total Orders Amount": {
        "data_source": "orders",
        "operation": "sum_amount",
        "filters": ["date_range", "state", "exemption"],
        "result_type": "decimal",
        "description": "Sum of all order amounts (optionally filtered)",
    },
    "Total Refunds Amount": {
        "data_source": "refunds",
        "operation": "sum_amount",
        "filters": ["date_range", "state"],
        "result_type": "decimal",
        "description": "Sum of all refund amounts (optionally filtered)",
    },
    "Total Orders Sales Tax": {
        "data_source": "orders",
        "operation": "sum_sales_tax",
        "filters": ["date_range", "state", "exemption"],
        "result_type": "decimal",
        "description": "Sum of sales tax collected on orders (optionally filtered)",
    },
    "Total Refunds Sales Tax": {
        "data_source": "refunds",
        "operation": "sum_sales_tax",
        "filters": ["date_range", "state"],
        "result_type": "decimal",
        "description": "Sum of sales tax refunded (optionally filtered)",
    },
    # ZIP/City filtered checks
    "Order Count by ZIP": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["date_range", "zip"],
        "result_type": "integer",
        "description": "Count orders shipped to a specific ZIP code (to_zip column)",
    },
    "Order Count by City": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["date_range", "city"],
        "result_type": "integer",
        "description": "Count orders shipped to a specific city (to_city column)",
    },
    "Total Orders Amount by ZIP": {
        "data_source": "orders",
        "operation": "sum_amount",
        "filters": ["date_range", "zip"],
        "result_type": "decimal",
        "description": "Sum of order amounts shipped to a specific ZIP (to_zip column)",
    },
    "Total Orders Amount by City": {
        "data_source": "orders",
        "operation": "sum_amount",
        "filters": ["date_range", "city"],
        "result_type": "decimal",
        "description": "Sum of order amounts shipped to a specific city (to_city column)",
    },
    "Total Orders Sales Tax by ZIP": {
        "data_source": "orders",
        "operation": "sum_sales_tax",
        "filters": ["date_range", "zip"],
        "result_type": "decimal",
        "description": "Sum of sales tax for orders to a specific ZIP (to_zip column)",
    },
    "Total Orders Sales Tax by City": {
        "data_source": "orders",
        "operation": "sum_sales_tax",
        "filters": ["date_range", "city"],
        "result_type": "decimal",
        "description": "Sum of sales tax for orders to a specific city (to_city column)",
    },
    # Individual order field checks
    "Order Field Value": {
        "data_source": "orders",
        "operation": "get_field",
        "required_params": ["transaction_id", "field_name"],
        "result_type": "dynamic",
        "description": "Get a specific field value from an order (e.g., shipping, amount, sales_tax)",
    },
    "Refund Field Value": {
        "data_source": "refunds",
        "operation": "get_field",
        "required_params": ["transaction_id", "field_name"],
        "result_type": "dynamic",
        "description": "Get a specific field value from a refund",
    },
    # Rate checks
    "Rate for ZIP": {
        "data_source": "rates",
        "operation": "get_rate_field",
        "required_params": ["zip_code", "rate_field"],
        "result_type": "decimal",
        "description": "Get a rate field value for a specific ZIP (e.g., combined_rate, state_rate)",
    },
    # ====================
    # LINE ITEM CHECKS
    # ====================
    "Line Item Tax Code": {
        "data_source": "orders",
        "operation": "check_line_item_tax_code",
        "required_params": ["transaction_id", "tax_code"],
        "result_type": "boolean",
        "description": "Check if an order has a line item with a specific product_tax_code. Common codes: 20010 (clothing), 30070 (food), 81112 (digital goods)",
    },
    "Line Item Count": {
        "data_source": "orders",
        "operation": "count_line_items",
        "required_params": ["transaction_id"],
        "result_type": "integer",
        "description": "Count the number of line items in an order",
    },
    "Line Item with Tax Code Count": {
        "data_source": "orders",
        "operation": "count_line_items_with_tax_code",
        "required_params": ["tax_code"],
        "filters": ["date_range", "state"],
        "result_type": "integer",
        "description": "Count line items across orders that have a specific product_tax_code",
    },
    # ====================
    # DATA INTEGRITY CHECKS
    # ====================
    "Refund Linked to Order": {
        "data_source": "refunds",
        "operation": "check_refund_linked",
        "required_params": ["refund_transaction_id", "order_transaction_id"],
        "result_type": "boolean",
        "description": "Check if a refund's transaction_reference_id matches the expected order transaction_id",
    },
    "Unique Transaction IDs": {
        "data_source": "orders",
        "operation": "check_unique_ids",
        "result_type": "boolean",
        "description": "Check that there are no duplicate transaction_ids in orders",
    },
    "Has Required Fields": {
        "data_source": "orders",
        "operation": "check_required_fields",
        "required_params": ["transaction_id", "required_fields"],
        "result_type": "boolean",
        "description": "Check if an order has all required fields populated. required_fields is comma-separated (e.g., 'amount,to_state,to_zip')",
    },
    "Date Sequence Valid": {
        "data_source": "refunds",
        "operation": "check_date_sequence",
        "required_params": ["refund_transaction_id"],
        "result_type": "boolean",
        "description": "Check if refund date >= linked order date (validates proper sequence)",
    },
    # ====================
    # TRANSACTION COUNTS
    # ====================
    "Transaction Count by State": {
        "data_source": "orders",
        "operation": "count",
        "filters": ["date_range", "state"],
        "result_type": "integer",
        "description": "Count orders to a specific state (for economic nexus threshold tracking)",
    },
    "Unique States Count": {
        "data_source": "orders",
        "operation": "count_unique_states",
        "filters": ["date_range"],
        "result_type": "integer",
        "description": "Count unique destination states in orders (how many states need filing)",
    },
    "Unique ZIPs Count": {
        "data_source": "orders",
        "operation": "count_unique_zips",
        "filters": ["date_range", "state"],
        "result_type": "integer",
        "description": "Count unique destination ZIP codes (useful for jurisdiction analysis)",
    },
    # ====================
    # TAX VERIFICATION CHECKS
    # ====================
    "Effective Tax Rate": {
        "data_source": "orders",
        "operation": "calc_effective_rate",
        "required_params": ["transaction_id"],
        "result_type": "decimal",
        "description": "Calculate effective tax rate for an order: sales_tax / amount (validates rate applied)",
    },
    "Tax Rate Matches Expected": {
        "data_source": "orders",
        "operation": "check_rate_matches",
        "required_params": ["transaction_id", "expected_rate"],
        "result_type": "boolean",
        "description": "Check if an order's effective rate matches expected rate (within 0.1% tolerance)",
    },
}


def _apply_filters(
    transactions: list[dict[str, Any]],
    field_config: dict[str, Any],
    verifier_values: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply configured filters to transactions.

    Args:
        transactions: List of transactions to filter
        field_config: Field configuration with filter specs
        verifier_values: User-provided filter values

    Returns:
        Filtered list of transactions
    """
    filters = field_config.get("filters", [])
    result = transactions

    # Handle quarter-based date range filters
    for f in filters:
        if f.startswith("quarter_"):
            quarter = int(f.split("_")[1])
            year = verifier_values.get("year", 2025)
            from_date, to_date = get_quarter_date_range(year, quarter)
            result = filter_transactions_by_date_range(result, from_date, to_date)

    # Handle explicit date range filter
    if "date_range" in filters:
        from_date = verifier_values.get("from_date")
        to_date = verifier_values.get("to_date")
        if from_date or to_date:
            result = filter_transactions_by_date_range(result, from_date, to_date)

    # Handle state filter
    if "state" in filters:
        state_code = verifier_values.get("state_code")
        if state_code:
            result = filter_transactions_by_state(result, state_code)

    # Handle exemption filter
    if "exemption" in filters:
        exemption_type = verifier_values.get("exemption_type")
        exclude_exempt = verifier_values.get("exclude_exempt", False)
        if exemption_type or exclude_exempt:
            result = filter_transactions_by_exemption_type(
                result, exemption_type, exclude_exempt
            )

    # Handle ZIP filter
    if "zip" in filters:
        zip_code = verifier_values.get("zip_code")
        if zip_code:
            result = filter_transactions_by_zip(result, zip_code)

    # Handle city filter
    if "city" in filters:
        city_name = verifier_values.get("city_name")
        if city_name:
            result = filter_transactions_by_city(result, city_name)

    return result


def _execute_operation(
    data: list[dict[str, Any]] | dict[str, Any],
    field_config: dict[str, Any],
    verifier_values: dict[str, Any],
    full_state: dict[str, Any] | None = None,
) -> Any:
    """Execute the configured operation on the data.

    Uses generic utilities from generic.py for flexibility.

    Args:
        data: Data to operate on (transactions list or other structure)
        field_config: Field configuration with operation spec
        verifier_values: User-provided parameter values
        full_state: Optional full TaxJar state for cross-data operations

    Returns:
        Result of the operation
    """
    operation = field_config["operation"]

    # Helper to ensure data is a list for list operations
    def as_list() -> list[dict[str, Any]]:
        return data if isinstance(data, list) else []

    if operation == "count":
        return len(data)

    elif operation == "exists":
        transaction_id = str(verifier_values.get("transaction_id") or "")
        return find_transaction_by_id(as_list(), transaction_id) is not None

    elif operation == "region_exists":
        region_code = str(verifier_values.get("region_code") or "")
        return find_nexus_region(as_list(), region_code) is not None

    elif operation == "get_region_code":
        region_index = int(verifier_values.get("region_index", 0) or 0)
        data_list = as_list()
        if 0 <= region_index < len(data_list):
            return get_nested_field(data_list[region_index], "region_code") or ""
        return None

    elif operation == "list_region_codes":
        codes = [get_nested_field(r, "region_code") or "" for r in as_list()]
        return ", ".join(str(c) for c in codes)

    elif operation == "sum_amount":
        # Use generic aggregate_field
        return aggregate_field(as_list(), "amount", "sum")

    elif operation == "sum_sales_tax":
        # Use generic aggregate_field
        return aggregate_field(as_list(), "sales_tax", "sum")

    elif operation == "get_field":
        # Get a specific field value from a transaction (supports dot notation)
        transaction_id = str(verifier_values.get("transaction_id") or "")
        field_name = str(verifier_values.get("field_name") or "")
        return get_transaction_field(as_list(), transaction_id, field_name)

    elif operation == "get_rate_field":
        # Get a rate field for a specific ZIP
        zip_code = str(verifier_values.get("zip_code") or "")
        rate_field = str(verifier_values.get("rate_field", "combined_rate") or "")
        rate_record = get_rate_for_zip(as_list(), zip_code)
        if rate_record:
            return get_nested_field(rate_record, rate_field)
        return None

    # ====================
    # LINE ITEM OPERATIONS
    # ====================

    elif operation == "check_line_item_tax_code":
        # Check if order has line item with specific tax code
        transaction_id = str(verifier_values.get("transaction_id") or "")
        tax_code = str(verifier_values.get("tax_code") or "")
        txn = find_transaction_by_id(as_list(), transaction_id)
        if not txn:
            return False
        line_items = txn.get("line_items", [])
        if not isinstance(line_items, list):
            return False
        for item in line_items:
            if isinstance(item, dict):
                item_code = item.get("product_tax_code", "")
                if str(item_code) == tax_code:
                    return True
        return False

    elif operation == "count_line_items":
        # Count line items in a specific order
        transaction_id = str(verifier_values.get("transaction_id") or "")
        txn = find_transaction_by_id(as_list(), transaction_id)
        if not txn:
            return 0
        line_items = txn.get("line_items", [])
        return len(line_items) if isinstance(line_items, list) else 0

    elif operation == "count_line_items_with_tax_code":
        # Count line items across all orders with specific tax code
        tax_code = str(verifier_values.get("tax_code") or "")
        count = 0
        for txn in as_list():
            line_items = txn.get("line_items", [])
            if isinstance(line_items, list):
                for item in line_items:
                    if isinstance(item, dict):
                        item_code = item.get("product_tax_code", "")
                        if str(item_code) == tax_code:
                            count += 1
        return count

    # ====================
    # DATA INTEGRITY OPERATIONS
    # ====================

    elif operation == "check_refund_linked":
        # Check if refund is linked to correct order
        refund_id = str(verifier_values.get("refund_transaction_id") or "")
        expected_order_id = str(verifier_values.get("order_transaction_id") or "")
        refund = find_transaction_by_id(as_list(), refund_id)
        if not refund:
            return False
        actual_ref = refund.get("transaction_reference_id", "")
        return str(actual_ref) == expected_order_id

    elif operation == "check_unique_ids":
        # Check for duplicate transaction IDs
        ids = [
            txn.get("transaction_id") for txn in as_list() if txn.get("transaction_id")
        ]
        return len(ids) == len(set(ids))

    elif operation == "check_required_fields":
        # Check if order has all required fields
        transaction_id = str(verifier_values.get("transaction_id") or "")
        required_fields_str = str(verifier_values.get("required_fields", "") or "")
        required_fields = [
            f.strip() for f in required_fields_str.split(",") if f.strip()
        ]
        txn = find_transaction_by_id(as_list(), transaction_id)
        if not txn:
            return False
        for field in required_fields:
            value = get_nested_field(txn, field)
            if value is None or value == "":
                return False
        return True

    elif operation == "check_date_sequence":
        # Check if refund date >= order date
        refund_id = str(verifier_values.get("refund_transaction_id") or "")
        refund = find_transaction_by_id(as_list(), refund_id)
        if not refund:
            return False

        order_ref = refund.get("transaction_reference_id")
        if not order_ref:
            return True  # No linked order, sequence is valid

        refund_date_str = str(refund.get("transaction_date", "") or "")
        if not refund_date_str:
            return False  # Refund has no date, can't validate

        # Get orders from full_state to find the linked order
        if not full_state:
            return False  # Can't validate without orders data

        orders_data = full_state.get("orders", [])
        orders_list = orders_data if isinstance(orders_data, list) else []
        order = find_transaction_by_id(orders_list, str(order_ref))
        if not order:
            return False  # Linked order not found

        order_date_str = str(order.get("transaction_date", "") or "")
        if not order_date_str:
            return False  # Order has no date, can't validate

        # Parse and compare dates
        try:
            refund_date = parse_transaction_date(refund_date_str)
            order_date = parse_transaction_date(order_date_str)
            return refund_date >= order_date
        except (ValueError, TypeError):
            return False  # Invalid date format

    # ====================
    # COUNT OPERATIONS
    # ====================

    elif operation == "count_unique_states":
        # Count unique destination states
        states: set[str] = set()
        for txn in as_list():
            state = txn.get("to_state")
            if state:
                states.add(str(state).upper())
        return len(states)

    elif operation == "count_unique_zips":
        # Count unique destination ZIPs
        zips: set[str] = set()
        for txn in as_list():
            zip_code = txn.get("to_zip")
            if zip_code:
                zips.add(str(zip_code))
        return len(zips)

    # ====================
    # TAX VERIFICATION OPERATIONS
    # ====================

    elif operation == "calc_effective_rate":
        # Calculate effective tax rate for an order
        transaction_id = str(verifier_values.get("transaction_id") or "")
        txn = find_transaction_by_id(as_list(), transaction_id)
        if not txn:
            return None
        try:
            amount = Decimal(str(txn.get("amount", 0)))
            sales_tax = Decimal(str(txn.get("sales_tax", 0)))
            if amount == 0:
                return Decimal("0")
            return sales_tax / amount
        except (ValueError, TypeError):
            return None

    elif operation == "check_rate_matches":
        # Check if effective rate matches expected
        transaction_id = str(verifier_values.get("transaction_id") or "")
        expected_rate = Decimal(str(verifier_values.get("expected_rate", 0) or 0))
        txn = find_transaction_by_id(as_list(), transaction_id)
        if not txn:
            return False
        try:
            amount = Decimal(str(txn.get("amount", 0)))
            sales_tax = Decimal(str(txn.get("sales_tax", 0)))
            if amount == 0:
                return expected_rate == 0
            effective_rate = sales_tax / amount
            # Allow 0.1% tolerance
            tolerance = Decimal("0.001")
            return abs(effective_rate - expected_rate) <= tolerance
        except (ValueError, TypeError):
            return False

    else:
        raise ValueError(f"Unknown operation: {operation}")


async def taxjar_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any TaxJar field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically applies filters and evaluates the result.
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

    # Validate required parameters
    required_params = field_config.get("required_params", [])
    for param in required_params:
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

    # Get TaxJar state from helper
    taxjar_state = input.helper_results.get(HelperIds.TAXJAR_STATE)
    if not taxjar_state:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="TaxJar state not available from helper",
        )

    # Get the appropriate data source
    data_source = field_config["data_source"]
    data = taxjar_state.get(data_source, [])

    # Apply filters if this is a transaction check
    if data_source in ["orders", "refunds"] and isinstance(data, list):
        data = _apply_filters(data, field_config, input.verifier.verifier_values)

    # Execute the operation
    try:
        result = _execute_operation(
            data, field_config, input.verifier.verifier_values, full_state=taxjar_state
        )

        logger.info(
            f"TaxJar field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing field check for {field_to_check}: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Field check failed: {str(e)}",
        )

    # Compare based on result_type
    result_type = field_config["result_type"]

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "decimal":
        # Get tolerance with validation
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
        actual_bool = bool(result)
        expected_bool = str(expected_value).lower() in ["true", "1", "yes"]
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'} {'✓' if matches else '✗'}"
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

        # If no exact match, try parsing as dates (handles "2025-09-05T00:00:00" vs "2025-09-05")
        if not matches:
            try:
                result_date = parse_transaction_date(str(result))
                expected_date = parse_transaction_date(str(expected_value))
                matches = result_date.date() == expected_date.date()
            except (ValueError, TypeError):
                pass  # Not valid dates, keep matches as False

        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    else:
        # Dynamic/unknown types - try exact match first, then date parsing
        matches = str(result) == str(expected_value)

        if not matches:
            try:
                result_date = parse_transaction_date(str(result))
                expected_date = parse_transaction_date(str(expected_value))
                matches = result_date.date() == expected_date.date()
            except (ValueError, TypeError):
                pass  # Not valid dates, keep matches as False

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
