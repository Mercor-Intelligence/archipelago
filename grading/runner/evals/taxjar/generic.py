"""Generic TaxJar verifiers for flexible grading.

These verifiers provide maximum flexibility to handle diverse rubric patterns
without needing to add new hardcoded operations for each new task.
"""

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

logger = logging.getLogger(__name__)


# ============================================================================
# SAFE CONVERSION UTILITIES
# ============================================================================


def safe_decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    """Safely convert a value to Decimal.

    Handles common edge cases that cause InvalidOperation errors:
    - Empty strings
    - None values
    - Non-numeric strings like "N/A", "null", "-", etc.

    Args:
        value: Value to convert
        default: Default value to return if conversion fails (default: None)

    Returns:
        Decimal value or default if conversion fails
    """
    if value is None:
        return default

    try:
        str_value = str(value).strip()
        if not str_value or str_value.lower() in (
            "",
            "n/a",
            "null",
            "none",
            "-",
            "nan",
        ):
            return default
        return Decimal(str_value)
    except (InvalidOperation, ValueError, TypeError):
        return default


# ============================================================================
# GENERIC FIELD ACCESS
# ============================================================================


def get_nested_field(obj: dict[str, Any] | None, path: str) -> Any:
    """Get a nested field value using dot notation.

    Args:
        obj: Dictionary to traverse (or None)
        path: Dot-separated path (e.g., "order.line_items.0.product_tax_code")

    Returns:
        Field value or None if not found

    Examples:
        get_nested_field({"a": {"b": 1}}, "a.b") -> 1
        get_nested_field({"items": [{"x": 1}]}, "items.0.x") -> 1
        get_nested_field(None, "a.b") -> None
        get_nested_field({"a": 1}, "") -> {"a": 1}  # Empty path returns obj
    """
    if obj is None:
        return None
    if not path:
        return obj

    current = obj
    for part in path.split("."):
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


def find_records_matching(
    records: list[dict[str, Any]],
    criteria: dict[str, Any],
) -> list[dict[str, Any]]:
    """Find all records matching the given criteria.

    Args:
        records: List of records to search
        criteria: Dictionary of field -> expected value pairs

    Returns:
        List of matching records

    Examples:
        find_records_matching(orders, {"to_state": "CA", "exemption_type": ""})
        find_records_matching(customers, {"exemption_type": "wholesale"})
    """
    matches = []
    for record in records:
        match = True
        for field, expected in criteria.items():
            actual = get_nested_field(record, field)
            # Handle case-insensitive string comparison
            if isinstance(expected, str) and isinstance(actual, str):
                if actual.lower() != expected.lower():
                    match = False
                    break
            elif actual != expected:
                match = False
                break
        if match:
            matches.append(record)
    return matches


def aggregate_field(
    records: list[dict[str, Any]],
    field: str,
    operation: str = "sum",
) -> Decimal:
    """Aggregate a field across records.

    Args:
        records: List of records
        field: Field name to aggregate
        operation: "sum", "count", "avg", "min", "max"

    Returns:
        Aggregated value as Decimal
    """
    values = []
    for record in records:
        val = get_nested_field(record, field)
        if val is not None:
            try:
                values.append(Decimal(str(val)))
            except (ValueError, TypeError):
                pass

    if not values:
        return Decimal("0")

    if operation == "sum":
        return sum(values, Decimal("0"))
    elif operation == "count":
        return Decimal(len(values))
    elif operation == "avg":
        return sum(values, Decimal("0")) / Decimal(len(values))
    elif operation == "min":
        return min(values)
    elif operation == "max":
        return max(values)
    else:
        return Decimal("0")


# ============================================================================
# EXPRESSION EVALUATOR
# ============================================================================


def evaluate_expression(
    expression: str,
    variables: dict[str, Any],
) -> Decimal:
    """Evaluate a mathematical expression with variables.

    Args:
        expression: Expression like "orders_amount - refunds_amount"
        variables: Dictionary of variable names to values

    Returns:
        Evaluated result as Decimal

    Examples:
        evaluate_expression("a + b", {"a": 100, "b": 50}) -> 150
        evaluate_expression("amount * rate", {"amount": 1000, "rate": 0.0975}) -> 97.5
    """
    # Convert all variables to Decimal
    decimal_vars = {}
    for name, value in variables.items():
        if value is None:
            decimal_vars[name] = Decimal("0")
        elif isinstance(value, int | float | str | Decimal):
            decimal_vars[name] = Decimal(str(value))
        else:
            decimal_vars[name] = Decimal("0")

    # Replace variable names with their values
    # Use word boundaries to avoid partial matches
    result_expr = expression
    for name, value in decimal_vars.items():
        result_expr = re.sub(
            rf"\b{re.escape(name)}\b",
            f"Decimal('{value}')",
            result_expr,
        )

    # Evaluate safely
    try:
        # Only allow safe operations
        allowed_names = {"Decimal": Decimal, "abs": abs, "min": min, "max": max}
        result = eval(result_expr, {"__builtins__": {}}, allowed_names)
        return Decimal(str(result))
    except Exception as e:
        logger.error(f"Expression evaluation failed: {expression} -> {e}")
        return Decimal("0")


# ============================================================================
# GENERIC VERIFIER IMPLEMENTATIONS
# ============================================================================


async def taxjar_generic_field_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any field on any record type using flexible path notation.

    Config:
        record_type: "orders", "refunds", "customers", "rates", "nexus_regions"
        record_filter: {"field": "value"} - criteria to find the record
        field_path: Dot-notation path to the field (e.g., "line_items.0.product_tax_code")
        expected_value: Expected field value (string, number, boolean)
        comparison: "equals", "contains", "greater_than", "less_than", "exists", "not_exists"

    Examples:
        - Check if order SUN-1001 has shipping = 100:
          record_type="orders", record_filter={"transaction_id": "SUN-1001"},
          field_path="shipping", expected_value="100.00"

        - Check if customer CUST-001 has exemption_type = "wholesale":
          record_type="customers", record_filter={"customer_id": "CUST-001"},
          field_path="exemption_type", expected_value="wholesale"

        - Check if order has a line item with product_tax_code = "20010":
          record_type="orders", record_filter={"transaction_id": "ORD-001"},
          field_path="line_items.0.product_tax_code", expected_value="20010"
    """
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    taxjar_state = input.helper_results.get(HelperIds.TAXJAR_STATE)
    if not taxjar_state:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="TaxJar state not available",
        )

    # Get configuration
    values = input.verifier.verifier_values
    record_type = values.get("record_type", "orders")
    record_filter_str = values.get("record_filter", "{}")
    field_path = values.get("field_path", "")
    expected_value = values.get("expected_value")
    comparison = values.get("comparison", "equals")

    # Parse record filter
    try:
        record_filter = (
            json.loads(record_filter_str)
            if isinstance(record_filter_str, str)
            else record_filter_str
        )
    except json.JSONDecodeError:
        record_filter = {}

    # Get records of the specified type
    records = taxjar_state.get(record_type, [])

    # Find matching records
    matching = find_records_matching(records, record_filter)

    if not matching:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"filter": record_filter, "found": 0},
            message=f"No {record_type} matching filter: {record_filter}",
        )

    # Get the field value from the first match
    record = matching[0]
    actual_value = get_nested_field(record, field_path)

    # Perform comparison
    passed = False
    if comparison == "equals":
        if isinstance(expected_value, str) and isinstance(actual_value, str):
            passed = actual_value.lower() == expected_value.lower()
        else:
            # Try numeric comparison
            try:
                passed = Decimal(str(actual_value)) == Decimal(str(expected_value))
            except (ValueError, TypeError):
                passed = actual_value == expected_value
    elif comparison == "contains":
        passed = str(expected_value).lower() in str(actual_value).lower()
    elif comparison == "greater_than":
        try:
            passed = Decimal(str(actual_value)) > Decimal(str(expected_value))
        except (ValueError, TypeError):
            passed = False
    elif comparison == "less_than":
        try:
            passed = Decimal(str(actual_value)) < Decimal(str(expected_value))
        except (ValueError, TypeError):
            passed = False
    elif comparison == "exists":
        passed = actual_value is not None
    elif comparison == "not_exists":
        passed = actual_value is None

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values={
            "record_type": record_type,
            "field_path": field_path,
            "actual_value": str(actual_value) if actual_value else None,
            "expected_value": str(expected_value) if expected_value else None,
            "comparison": comparison,
        },
        message=(
            f"✓ {record_type}.{field_path} = {actual_value}"
            if passed
            else f"✗ {record_type}.{field_path}: expected {expected_value}, got {actual_value}"
        ),
    )


async def taxjar_expression_eval(input: EvalImplInput) -> VerifierResult:
    """Evaluate a custom expression against TaxJar data.

    This verifier allows writing custom formulas to calculate any value.

    Config:
        expression: Mathematical expression using variable names
        variables: JSON object mapping variable names to aggregation specs
        expected_value: Expected result
        tolerance: Acceptable variance (default 0.01)

    Variable spec format:
        {
            "var_name": {
                "source": "orders" | "refunds" | "rates" | etc.,
                "filter": {"field": "value"},  # optional
                "field": "amount" | "sales_tax" | etc.,
                "operation": "sum" | "count" | "avg" | "min" | "max"
            }
        }

    Examples:
        - Net taxable sales for LA:
          expression="order_amount - refund_amount"
          variables={
              "order_amount": {"source": "orders", "filter": {"to_zip": "90002"}, "field": "amount", "operation": "sum"},
              "refund_amount": {"source": "refunds", "filter": {"to_zip": "90002"}, "field": "amount", "operation": "sum"}
          }

        - Corrected tax with rate:
          expression="net_amount * corrected_rate"
          variables={
              "net_amount": {...},
              "corrected_rate": {"value": 0.0975}  # Direct value
          }

        - Blended rate:
          expression="total_tax / total_amount"
          variables={...}
    """
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    taxjar_state = input.helper_results.get(HelperIds.TAXJAR_STATE)
    if not taxjar_state:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="TaxJar state not available",
        )

    values = input.verifier.verifier_values
    expression = values.get("expression", "")
    variables_str = values.get("variables", "{}")
    expected_value = safe_decimal(values.get("expected_value"), Decimal("0"))
    tolerance = safe_decimal(values.get("tolerance"), Decimal("0.01"))
    if expected_value is None:
        expected_value = Decimal("0")
    if tolerance is None:
        tolerance = Decimal("0.01")

    # Parse variables spec
    try:
        variables_spec = (
            json.loads(variables_str)
            if isinstance(variables_str, str)
            else variables_str
        )
    except json.JSONDecodeError:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Invalid variables JSON: {variables_str}",
        )

    # Evaluate each variable
    computed_vars = {}
    var_details = {}
    for var_name, spec in variables_spec.items():
        if "value" in spec:
            # Direct value
            decimal_val = safe_decimal(spec["value"], Decimal("0"))
            if decimal_val is None:
                decimal_val = Decimal("0")
            computed_vars[var_name] = decimal_val
            var_details[var_name] = {"type": "direct", "value": float(decimal_val)}
        else:
            # Aggregation spec
            source = spec.get("source", "orders")
            filter_criteria = spec.get("filter", {})
            field = spec.get("field", "amount")
            operation = spec.get("operation", "sum")

            records = taxjar_state.get(source, [])
            if filter_criteria:
                records = find_records_matching(records, filter_criteria)

            result = aggregate_field(records, field, operation)
            computed_vars[var_name] = result
            var_details[var_name] = {
                "type": "aggregation",
                "source": source,
                "filter": filter_criteria,
                "field": field,
                "operation": operation,
                "record_count": len(records),
                "value": float(result),
            }

    # Evaluate expression
    calculated_value = evaluate_expression(expression, computed_vars)

    # Compare
    variance = abs(calculated_value - expected_value)
    passed = variance <= tolerance

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values={
            "expression": expression,
            "variables": var_details,
            "calculated_value": float(calculated_value),
            "expected_value": float(expected_value),
            "variance": float(variance),
            "tolerance": float(tolerance),
        },
        message=(
            f"✓ {expression} = ${float(calculated_value):,.2f}"
            if passed
            else f"✗ {expression} = ${float(calculated_value):,.2f} (expected ${float(expected_value):,.2f})"
        ),
    )


async def taxjar_multi_state_eval(input: EvalImplInput) -> VerifierResult:
    """Verify calculations across multiple states/ZIPs.

    Config:
        calculation: "net_taxable", "tax_collected", "tax_liability", "tax_rate_check"
        group_by: "to_state", "to_zip", "to_city"
        expected_values: JSON object mapping group -> expected value
        tolerance: Acceptable variance per group

    Examples:
        - Verify net taxable by city:
          calculation="net_taxable"
          group_by="to_city"
          expected_values={"Los Angeles": 14500, "San Diego": 10700, "San Francisco": 6200}
    """
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    taxjar_state = input.helper_results.get(HelperIds.TAXJAR_STATE)
    if not taxjar_state:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="TaxJar state not available",
        )

    values = input.verifier.verifier_values
    calculation = values.get("calculation", "net_taxable")
    group_by = values.get("group_by", "to_state")
    expected_values_str = values.get("expected_values", "{}")
    tolerance = safe_decimal(values.get("tolerance"), Decimal("0.01"))
    if tolerance is None:
        tolerance = Decimal("0.01")

    try:
        expected_values = (
            json.loads(expected_values_str)
            if isinstance(expected_values_str, str)
            else expected_values_str
        )
    except json.JSONDecodeError:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Invalid expected_values JSON: {expected_values_str}",
        )

    orders = taxjar_state.get("orders", [])
    refunds = taxjar_state.get("refunds", [])

    # Group orders and refunds
    order_groups: dict[str, list[dict[str, Any]]] = {}
    refund_groups: dict[str, list[dict[str, Any]]] = {}

    for order in orders:
        group_key = str(order.get(group_by, "unknown"))
        if group_key not in order_groups:
            order_groups[group_key] = []
        order_groups[group_key].append(order)

    for refund in refunds:
        group_key = str(refund.get(group_by, "unknown"))
        if group_key not in refund_groups:
            refund_groups[group_key] = []
        refund_groups[group_key].append(refund)

    # Calculate for each group
    results = {}
    all_passed = True
    messages = []

    for group_key, expected in expected_values.items():
        group_orders = order_groups.get(group_key, [])
        group_refunds = refund_groups.get(group_key, [])

        if calculation == "net_taxable":
            order_amount = aggregate_field(group_orders, "amount", "sum")
            refund_amount = aggregate_field(group_refunds, "amount", "sum")
            calculated = order_amount - refund_amount
        elif calculation == "tax_collected":
            calculated = aggregate_field(group_orders, "sales_tax", "sum")
        elif calculation == "tax_liability":
            order_tax = aggregate_field(group_orders, "sales_tax", "sum")
            refund_tax = aggregate_field(group_refunds, "sales_tax", "sum")
            calculated = order_tax - refund_tax
        else:
            calculated = Decimal("0")

        expected_dec = Decimal(str(expected))
        variance = abs(calculated - expected_dec)
        passed = variance <= tolerance

        results[group_key] = {
            "calculated": float(calculated),
            "expected": float(expected_dec),
            "variance": float(variance),
            "passed": passed,
        }

        if passed:
            messages.append(f"✓ {group_key}: ${float(calculated):,.2f}")
        else:
            messages.append(
                f"✗ {group_key}: ${float(calculated):,.2f} (expected ${float(expected_dec):,.2f})"
            )
            all_passed = False

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if all_passed else 0.0,
        verifier_result_values={
            "calculation": calculation,
            "group_by": group_by,
            "results": results,
        },
        message=" | ".join(messages),
    )


async def taxjar_customer_exemption_eval(input: EvalImplInput) -> VerifierResult:
    """Verify customer exemptions were properly applied.

    Config:
        customer_id: Customer to check
        expected_exemption_type: Expected exemption type (wholesale, government, etc.)
        expected_exempt_regions: List of {"country": "US", "state": "CA"} objects
        check_orders: If true, verify orders for this customer have no tax

    Examples:
        - Verify customer CUST-001 is exempt in CA:
          customer_id="CUST-001"
          expected_exemption_type="wholesale"
          expected_exempt_regions=[{"country": "US", "state": "CA"}]
    """
    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    taxjar_state = input.helper_results.get(HelperIds.TAXJAR_STATE)
    if not taxjar_state:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="TaxJar state not available",
        )

    values = input.verifier.verifier_values
    customer_id = values.get("customer_id")
    expected_type = values.get("expected_exemption_type")
    expected_regions_str = values.get("expected_exempt_regions", "[]")
    check_orders = values.get("check_orders", False)

    try:
        expected_regions = (
            json.loads(expected_regions_str)
            if isinstance(expected_regions_str, str)
            else expected_regions_str
        )
    except json.JSONDecodeError:
        expected_regions = []

    customers = taxjar_state.get("customers", [])
    customer = None
    for c in customers:
        if c.get("customer_id") == customer_id:
            customer = c
            break

    if not customer:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Customer not found: {customer_id}",
        )

    # Check exemption type
    actual_type = customer.get("exemption_type", "")
    type_match = actual_type.lower() == expected_type.lower() if expected_type else True

    # Check exempt regions
    actual_regions = customer.get("exempt_regions", [])
    regions_match = True
    if expected_regions:
        for exp_region in expected_regions:
            found = False
            for act_region in actual_regions:
                if act_region.get("country") == exp_region.get(
                    "country"
                ) and act_region.get("state") == exp_region.get("state"):
                    found = True
                    break
            if not found:
                regions_match = False
                break

    # Check orders if requested
    orders_ok = True
    if check_orders:
        orders = taxjar_state.get("orders", [])
        for order in orders:
            if order.get("customer_id") == customer_id:
                # Exempt orders should have exemption_type set
                if not order.get("exemption_type"):
                    orders_ok = False
                    break

    passed = type_match and regions_match and orders_ok

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values={
            "customer_id": customer_id,
            "exemption_type": {
                "expected": expected_type,
                "actual": actual_type,
                "match": type_match,
            },
            "exempt_regions": {
                "expected": expected_regions,
                "actual": actual_regions,
                "match": regions_match,
            },
            "orders_checked": check_orders,
            "orders_ok": orders_ok,
        },
        message=(
            f"✓ Customer {customer_id} exemption verified"
            if passed
            else f"✗ Customer {customer_id} exemption mismatch"
        ),
    )
