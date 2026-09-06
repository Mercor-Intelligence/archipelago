"""TaxJar Calculation Verifier - Numerical calculation verification.

This verifier handles complex tax calculations including:
- Gross/net merchandise sales
- Sales tax collected/liability
- Projected values with growth rates
- Blended tax rates
- Incremental tax liability
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

from .generic import aggregate_field
from .utils import (
    calculate_blended_rate,
    calculate_expected_tax,
    calculate_incremental_liability,
    calculate_net_amount,
    calculate_net_sales_tax,
    calculate_projected_amount,
    calculate_projected_tax,
    filter_transactions_by_city,
    filter_transactions_by_date_range,
    filter_transactions_by_exemption_type,
    filter_transactions_by_state,
    filter_transactions_by_zip,
    get_quarter_date_range,
    sum_transaction_amounts,
    sum_transaction_sales_tax,
)

# Calculation type configurations
CALCULATION_TYPES: dict[str, dict[str, Any]] = {
    # Gross sales calculations
    "Gross Merchandise Sales": {
        "description": "Total order amounts (before refunds, optionally filtered by date/state)",
        "operation": "gross_sales",
        "filters": ["date_range", "state", "exemption"],
    },
    "Gross Merchandise Sales Including Shipping": {
        "description": "Total order amounts + shipping (before refunds)",
        "operation": "gross_sales_with_shipping",
        "filters": ["date_range", "state", "exemption"],
    },
    # Net sales calculations
    "Net Merchandise Sales": {
        "description": "Order amounts minus refund amounts (optionally filtered by date/state)",
        "operation": "net_sales",
        "filters": ["date_range", "state", "exemption"],
    },
    "Net Taxable Merchandise Sales": {
        "description": "Net sales excluding exempt transactions (e.g., wholesale). Formula: (Orders - Exempt Orders) - Refunds",
        "operation": "net_taxable_sales",
        "filters": ["date_range", "state"],
    },
    # Sales tax calculations
    "Sales Tax Collected": {
        "description": "Total sales tax collected on orders (before refunds)",
        "operation": "tax_collected",
        "filters": ["date_range", "state", "exemption"],
    },
    "Sales Tax Refunded": {
        "description": "Total sales tax refunded",
        "operation": "tax_refunded",
        "filters": ["date_range", "state"],
    },
    "Net Sales Tax Liability": {
        "description": "Sales tax collected minus sales tax refunded",
        "operation": "net_tax_liability",
        "filters": ["date_range", "state", "exemption"],
    },
    # Expected tax calculations
    "Expected Sales Tax": {
        "description": "Expected tax based on net sales × tax rate (for states without nexus where no tax was collected)",
        "operation": "expected_tax",
        "filters": ["date_range", "state"],
        "required_params": ["tax_rate"],
    },
    "Incremental Tax Liability": {
        "description": "Expected tax minus actual tax collected (amount owed when nexus was missed)",
        "operation": "incremental_liability",
        "filters": ["date_range", "state"],
        "required_params": ["tax_rate"],
    },
    # Blended rate calculation
    "Blended Tax Rate": {
        "description": "Effective tax rate = total tax collected / total taxable amount",
        "operation": "blended_rate",
        "filters": ["date_range", "state", "exemption"],
        "result_type": "percentage",
    },
    # Projection calculations
    "Projected Merchandise Sales": {
        "description": "Base net sales × (1 + growth_rate)",
        "operation": "projected_sales",
        "filters": ["date_range", "state", "exemption"],
        "required_params": ["growth_rate"],
    },
    "Projected Tax Liability": {
        "description": "Projected sales × fixed tax_rate",
        "operation": "projected_tax",
        "filters": ["date_range", "state", "exemption"],
        "required_params": ["growth_rate", "tax_rate"],
    },
    "Projected Tax Using Blended Rate": {
        "description": "Projected sales × blended_rate (calculated from corrected historical data)",
        "operation": "projected_tax_blended",
        "filters": ["date_range", "state", "exemption"],
        "required_params": ["growth_rate"],
    },
    # Corrected calculations (treating exempt transactions as taxable)
    "Corrected Net Taxable Sales": {
        "description": "Net sales treating ALL orders as taxable (ignores exemption_type). Formula: All Orders - Refunds",
        "operation": "corrected_net_taxable",
        "filters": ["date_range", "state"],
    },
    "Corrected Tax Liability": {
        "description": "Tax liability after adding missing tax on previously exempt orders. Formula: (Original Tax + Exempt Order Tax) - Refunded Tax",
        "operation": "corrected_tax_liability",
        "filters": ["date_range", "state"],
    },
    "Corrected Blended Tax Rate": {
        "description": "Corrected net tax liability / corrected net taxable sales",
        "operation": "corrected_blended_rate",
        "filters": ["date_range", "state"],
        "result_type": "percentage",
    },
    # Projection with corrected base (for criteria 13, 14)
    "Projected Corrected Merchandise Sales": {
        "description": "Corrected net taxable sales × (1 + growth_rate)",
        "operation": "projected_corrected_sales",
        "filters": ["date_range", "state"],
        "required_params": ["growth_rate"],
    },
    "Projected Tax Using Corrected Blended Rate": {
        "description": "Projected corrected sales × corrected blended rate",
        "operation": "projected_tax_corrected_blended",
        "filters": ["date_range", "state"],
        "required_params": ["growth_rate"],
    },
    # ZIP/City-based calculations (for Sunset Boards rubric)
    "Net Taxable Sales by ZIP": {
        "description": "Net taxable sales for orders shipped to a specific ZIP code. Uses 'to_zip' column.",
        "operation": "net_taxable_by_zip",
        "filters": ["date_range"],
        "required_params": ["zip_code"],
    },
    "Net Taxable Sales by City": {
        "description": "Net taxable sales for orders shipped to a specific city. Uses 'to_city' column.",
        "operation": "net_taxable_by_city",
        "filters": ["date_range"],
        "required_params": ["city_name"],
    },
    "Sales Tax Collected by ZIP": {
        "description": "Sales tax collected on orders to a specific ZIP (before refunds). Uses 'to_zip' column.",
        "operation": "tax_collected_by_zip",
        "filters": ["date_range"],
        "required_params": ["zip_code"],
    },
    "Sales Tax Collected by City": {
        "description": "Sales tax collected on orders to a specific city (before refunds). Uses 'to_city' column.",
        "operation": "tax_collected_by_city",
        "filters": ["date_range"],
        "required_params": ["city_name"],
    },
    "Net Sales Tax by ZIP": {
        "description": "Net sales tax (collected - refunded) for a specific ZIP. Uses 'to_zip' column.",
        "operation": "net_tax_by_zip",
        "filters": ["date_range"],
        "required_params": ["zip_code"],
    },
    "Net Sales Tax by City": {
        "description": "Net sales tax (collected - refunded) for a specific city. Uses 'to_city' column.",
        "operation": "net_tax_by_city",
        "filters": ["date_range"],
        "required_params": ["city_name"],
    },
    # Rate correction calculations
    "Corrected Tax with Rate": {
        "description": "Recalculate tax using a corrected rate. Formula: net_taxable × corrected_rate",
        "operation": "corrected_tax_with_rate",
        "filters": ["date_range"],
        "required_params": ["zip_code", "corrected_rate"],
    },
    "Additional Tax to Remit": {
        "description": "Difference between corrected tax and originally collected. Formula: corrected - original",
        "operation": "additional_tax_to_remit",
        "filters": ["date_range"],
        "required_params": ["zip_code", "corrected_rate"],
    },
    "Total Corrected Tax Liability": {
        "description": "Sum of all corrected taxes (some ZIPs corrected, others unchanged)",
        "operation": "total_corrected_liability",
        "filters": ["date_range", "state"],
    },
    # ====================
    # TAX CALCULATION VERIFICATION
    # ====================
    "Tax Calculation Match": {
        "description": "Compare recorded sales_tax against calculated (amount × expected_rate). Finds discrepancies.",
        "operation": "tax_calc_match",
        "filters": ["date_range", "state"],
        "required_params": ["expected_rate"],
        "result_type": "decimal",
    },
    "Tax Discrepancy Total": {
        "description": "Sum of all tax discrepancies: (amount × expected_rate) - recorded_sales_tax. Positive = undertaxed.",
        "operation": "tax_discrepancy_total",
        "filters": ["date_range", "state"],
        "required_params": ["expected_rate"],
    },
    # ====================
    # JURISDICTION BREAKDOWN
    # ====================
    "State Tax Component": {
        "description": "Sum of state_tax_collectable from tax calculations (if breakdown available)",
        "operation": "state_tax_component",
        "filters": ["date_range", "state"],
    },
    "County Tax Component": {
        "description": "Sum of county_tax_collectable from tax calculations (if breakdown available)",
        "operation": "county_tax_component",
        "filters": ["date_range", "state"],
    },
    "City Tax Component": {
        "description": "Sum of city_tax_collectable from tax calculations (if breakdown available)",
        "operation": "city_tax_component",
        "filters": ["date_range", "state"],
    },
    "Special District Tax Component": {
        "description": "Sum of special_district_tax_collectable from tax calculations (if breakdown available)",
        "operation": "special_district_tax_component",
        "filters": ["date_range", "state"],
    },
    # ====================
    # ECONOMIC NEXUS CALCULATIONS
    # ====================
    "Economic Nexus Sales Check": {
        "description": "Total sales to a state for nexus threshold comparison (typically $100K-$500K)",
        "operation": "nexus_sales_check",
        "filters": ["date_range"],
        "required_params": ["state_code", "threshold"],
        "result_type": "comparison",
    },
    "Economic Nexus Transaction Count Check": {
        "description": "Transaction count to a state for nexus threshold (typically 100-200 transactions)",
        "operation": "nexus_transaction_check",
        "filters": ["date_range"],
        "required_params": ["state_code", "threshold"],
        "result_type": "comparison",
    },
    # ====================
    # FILING CALCULATIONS
    # ====================
    "Filing Period Sales": {
        "description": "Total taxable sales for a filing period (month or quarter)",
        "operation": "filing_period_sales",
        "filters": ["date_range", "state"],
    },
    "Filing Period Tax Due": {
        "description": "Net tax due for a filing period (collected - refunded)",
        "operation": "filing_period_tax_due",
        "filters": ["date_range", "state"],
    },
    "Filing States Count": {
        "description": "Number of unique states requiring tax filing for the period",
        "operation": "filing_states_count",
        "filters": ["date_range"],
        "result_type": "integer",
    },
    # ====================
    # RATE VALIDATION
    # ====================
    "Average Effective Rate": {
        "description": "Average effective tax rate across orders: total_tax / total_amount",
        "operation": "avg_effective_rate",
        "filters": ["date_range", "state"],
        "result_type": "percentage",
    },
    "Rate Variance": {
        "description": "Difference between average effective rate and expected rate",
        "operation": "rate_variance",
        "filters": ["date_range", "state"],
        "required_params": ["expected_rate"],
        "result_type": "percentage",
    },
}


def _get_filtered_transactions(
    taxjar_state: dict[str, Any],
    data_source: str,
    verifier_values: dict[str, Any],
) -> list[dict[str, Any]]:
    """Get filtered transactions based on verifier configuration.

    Args:
        taxjar_state: TaxJar state dictionary with orders, refunds, etc.
        data_source: 'orders' or 'refunds'
        verifier_values: User-provided filter values

    Returns:
        Filtered list of transactions
    """
    transactions = taxjar_state.get(data_source, [])

    # Apply date range filter
    from_date = verifier_values.get("from_date")
    to_date = verifier_values.get("to_date")
    quarter = verifier_values.get("quarter")
    year = verifier_values.get("year", 2025)

    if quarter:
        from_date, to_date = get_quarter_date_range(year, int(quarter))

    if from_date or to_date:
        transactions = filter_transactions_by_date_range(
            transactions, from_date, to_date
        )

    # Apply state filter
    state_code = verifier_values.get("state_code")
    if state_code:
        transactions = filter_transactions_by_state(transactions, state_code)

    # Apply exemption filter
    exemption_type = verifier_values.get("exemption_type")
    exclude_exempt = verifier_values.get("exclude_exempt", False)
    if exemption_type or exclude_exempt:
        transactions = filter_transactions_by_exemption_type(
            transactions, exemption_type, exclude_exempt
        )

    return transactions


async def taxjar_calculation_eval(input: EvalImplInput) -> VerifierResult:
    """Verify TaxJar calculation results.

    Supports various tax calculations including gross/net sales, tax liability,
    projected amounts, and blended rates.
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

    # Get calculation type selection
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

    # Get expected value
    expected_value_raw = input.verifier.verifier_values.get("expected_value")
    if expected_value_raw is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    try:
        expected_value = Decimal(str(expected_value_raw))
    except (ValueError, TypeError, InvalidOperation) as e:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Invalid expected_value: {expected_value_raw} ({e})",
        )

    # Get calculation configuration
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

    # Validate required parameters
    required_params = calc_config.get("required_params", [])
    for param in required_params:
        if input.verifier.verifier_values.get(param) is None:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Required parameter '{param}' is missing for {calculation_type}",
            )

    # Get tolerance
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

    # Get filtered transactions
    orders = _get_filtered_transactions(
        taxjar_state, "orders", input.verifier.verifier_values
    )
    refunds = _get_filtered_transactions(
        taxjar_state, "refunds", input.verifier.verifier_values
    )

    # Execute the calculation
    operation = calc_config["operation"]
    calculated_value = Decimal("0")
    calculation_details = {}

    try:
        if operation == "gross_sales":
            calculated_value = sum_transaction_amounts(orders, include_shipping=False)
            calculation_details = {"order_count": len(orders)}

        elif operation == "gross_sales_with_shipping":
            calculated_value = sum_transaction_amounts(orders, include_shipping=True)
            calculation_details = {"order_count": len(orders)}

        elif operation == "net_sales":
            calculated_value = calculate_net_amount(
                orders, refunds, include_shipping=False
            )
            calculation_details = {
                "order_count": len(orders),
                "refund_count": len(refunds),
            }

        elif operation == "net_taxable_sales":
            # Exclude exempt orders for taxable sales
            taxable_orders = filter_transactions_by_exemption_type(
                orders, exclude_exempt=True
            )
            calculated_value = calculate_net_amount(
                taxable_orders, refunds, include_shipping=False
            )
            calculation_details = {
                "total_orders": len(orders),
                "taxable_orders": len(taxable_orders),
                "refund_count": len(refunds),
            }

        elif operation == "tax_collected":
            calculated_value = sum_transaction_sales_tax(orders)
            calculation_details = {"order_count": len(orders)}

        elif operation == "tax_refunded":
            calculated_value = sum_transaction_sales_tax(refunds)
            calculation_details = {"refund_count": len(refunds)}

        elif operation == "net_tax_liability":
            calculated_value = calculate_net_sales_tax(orders, refunds)
            calculation_details = {
                "order_count": len(orders),
                "refund_count": len(refunds),
                "tax_collected": float(sum_transaction_sales_tax(orders)),
                "tax_refunded": float(sum_transaction_sales_tax(refunds)),
            }

        elif operation == "expected_tax":
            tax_rate = Decimal(str(input.verifier.verifier_values["tax_rate"]))
            taxable_orders = filter_transactions_by_exemption_type(
                orders, exclude_exempt=True
            )
            net_taxable = calculate_net_amount(
                taxable_orders, refunds, include_shipping=False
            )
            calculated_value = calculate_expected_tax(net_taxable, tax_rate)
            calculation_details = {
                "net_taxable_amount": float(net_taxable),
                "tax_rate": float(tax_rate),
            }

        elif operation == "incremental_liability":
            tax_rate = Decimal(str(input.verifier.verifier_values["tax_rate"]))
            taxable_orders = filter_transactions_by_exemption_type(
                orders, exclude_exempt=True
            )
            net_taxable = calculate_net_amount(
                taxable_orders, refunds, include_shipping=False
            )
            expected_tax = calculate_expected_tax(net_taxable, tax_rate)
            actual_tax = sum_transaction_sales_tax(orders)
            calculated_value = calculate_incremental_liability(expected_tax, actual_tax)
            calculation_details = {
                "expected_tax": float(expected_tax),
                "actual_tax_collected": float(actual_tax),
            }

        elif operation == "blended_rate":
            total_tax = calculate_net_sales_tax(orders, refunds)
            net_taxable = calculate_net_amount(orders, refunds, include_shipping=False)
            calculated_value = calculate_blended_rate(total_tax, net_taxable)
            calculation_details = {
                "total_tax": float(total_tax),
                "net_taxable": float(net_taxable),
            }

        elif operation == "projected_sales":
            growth_rate = Decimal(str(input.verifier.verifier_values["growth_rate"]))
            base_amount = calculate_net_amount(orders, refunds, include_shipping=False)
            calculated_value = calculate_projected_amount(base_amount, growth_rate)
            calculation_details = {
                "base_amount": float(base_amount),
                "growth_rate": float(growth_rate),
            }

        elif operation == "projected_tax":
            growth_rate = Decimal(str(input.verifier.verifier_values["growth_rate"]))
            tax_rate = Decimal(str(input.verifier.verifier_values["tax_rate"]))
            base_amount = calculate_net_amount(orders, refunds, include_shipping=False)
            projected_sales = calculate_projected_amount(base_amount, growth_rate)
            calculated_value = calculate_projected_tax(projected_sales, tax_rate)
            calculation_details = {
                "base_amount": float(base_amount),
                "projected_sales": float(projected_sales),
                "tax_rate": float(tax_rate),
            }

        elif operation == "projected_tax_blended":
            growth_rate = Decimal(str(input.verifier.verifier_values["growth_rate"]))
            # Calculate blended rate from historical data
            total_tax = calculate_net_sales_tax(orders, refunds)
            net_taxable = calculate_net_amount(orders, refunds, include_shipping=False)
            blended_rate = calculate_blended_rate(total_tax, net_taxable)
            # Project forward
            projected_sales = calculate_projected_amount(net_taxable, growth_rate)
            calculated_value = calculate_projected_tax(projected_sales, blended_rate)
            calculation_details = {
                "blended_rate": float(blended_rate),
                "projected_sales": float(projected_sales),
            }

        elif operation == "corrected_net_taxable":
            # Treat ALL orders as taxable (ignore exemption_type)
            all_orders = taxjar_state.get("orders", [])
            # Re-filter by date/state only (not exemption)
            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
            state_code = input.verifier.verifier_values.get("state_code")
            if state_code:
                all_orders = filter_transactions_by_state(all_orders, state_code)

            calculated_value = calculate_net_amount(
                all_orders, refunds, include_shipping=False
            )
            calculation_details = {
                "order_count": len(all_orders),
                "refund_count": len(refunds),
            }

        elif operation == "corrected_tax_liability":
            # Calculate corrected tax: Original tax + tax on exempt orders - refunded tax
            # This treats previously exempt orders as now taxable
            all_orders = taxjar_state.get("orders", [])
            # Re-filter by date/state only
            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
            state_code = input.verifier.verifier_values.get("state_code")
            if state_code:
                all_orders = filter_transactions_by_state(all_orders, state_code)

            # Get taxable and exempt orders
            taxable_orders = filter_transactions_by_exemption_type(
                all_orders, exclude_exempt=True
            )
            exempt_orders = [o for o in all_orders if o not in taxable_orders]

            # Original tax collected (from taxable orders)
            original_tax = sum_transaction_sales_tax(taxable_orders)

            # Calculate blended rate from taxable orders
            original_taxable = sum_transaction_amounts(taxable_orders, False)
            blended_rate = calculate_blended_rate(original_tax, original_taxable)

            # Calculate tax that should have been collected on exempt orders
            exempt_amount = sum_transaction_amounts(exempt_orders, False)
            exempt_tax = exempt_amount * blended_rate

            # Tax refunded
            refund_tax = sum_transaction_sales_tax(refunds)

            # Corrected tax = original + exempt tax - refunded
            calculated_value = original_tax + exempt_tax - refund_tax
            calculation_details = {
                "original_tax_collected": float(original_tax),
                "exempt_order_amount": float(exempt_amount),
                "exempt_order_tax": float(exempt_tax),
                "blended_rate": float(blended_rate),
                "refund_tax": float(refund_tax),
            }

        elif operation == "corrected_blended_rate":
            # Corrected blended rate = corrected tax / corrected net taxable
            all_orders = taxjar_state.get("orders", [])
            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
            state_code = input.verifier.verifier_values.get("state_code")
            if state_code:
                all_orders = filter_transactions_by_state(all_orders, state_code)

            # Calculate corrected tax liability
            taxable_orders = filter_transactions_by_exemption_type(
                all_orders, exclude_exempt=True
            )
            original_tax = sum_transaction_sales_tax(taxable_orders)
            original_taxable = sum_transaction_amounts(taxable_orders, False)
            blended_rate = calculate_blended_rate(original_tax, original_taxable)
            exempt_orders = [o for o in all_orders if o not in taxable_orders]
            exempt_amount = sum_transaction_amounts(exempt_orders, False)
            exempt_tax = exempt_amount * blended_rate
            refund_tax = sum_transaction_sales_tax(refunds)
            corrected_tax = original_tax + exempt_tax - refund_tax

            # Corrected net taxable (all orders - refunds)
            corrected_net = calculate_net_amount(
                all_orders, refunds, include_shipping=False
            )

            calculated_value = calculate_blended_rate(corrected_tax, corrected_net)
            calculation_details = {
                "corrected_tax": float(corrected_tax),
                "corrected_net_taxable": float(corrected_net),
            }

        elif operation == "projected_corrected_sales":
            # Projected corrected net taxable sales
            growth_rate = Decimal(str(input.verifier.verifier_values["growth_rate"]))
            all_orders = taxjar_state.get("orders", [])
            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
            state_code = input.verifier.verifier_values.get("state_code")
            if state_code:
                all_orders = filter_transactions_by_state(all_orders, state_code)

            corrected_net = calculate_net_amount(
                all_orders, refunds, include_shipping=False
            )
            calculated_value = calculate_projected_amount(corrected_net, growth_rate)
            calculation_details = {
                "corrected_net_taxable": float(corrected_net),
                "growth_rate": float(growth_rate),
            }

        elif operation == "projected_tax_corrected_blended":
            # Projected tax using corrected blended rate
            growth_rate = Decimal(str(input.verifier.verifier_values["growth_rate"]))
            all_orders = taxjar_state.get("orders", [])
            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
            state_code = input.verifier.verifier_values.get("state_code")
            if state_code:
                all_orders = filter_transactions_by_state(all_orders, state_code)

            # Calculate corrected values
            taxable_orders = filter_transactions_by_exemption_type(
                all_orders, exclude_exempt=True
            )
            original_tax = sum_transaction_sales_tax(taxable_orders)
            original_taxable = sum_transaction_amounts(taxable_orders, False)
            blended_rate = calculate_blended_rate(original_tax, original_taxable)
            exempt_orders = [o for o in all_orders if o not in taxable_orders]
            exempt_amount = sum_transaction_amounts(exempt_orders, False)
            exempt_tax = exempt_amount * blended_rate
            refund_tax = sum_transaction_sales_tax(refunds)
            corrected_tax = original_tax + exempt_tax - refund_tax

            corrected_net = calculate_net_amount(
                all_orders, refunds, include_shipping=False
            )
            corrected_blended_rate = calculate_blended_rate(
                corrected_tax, corrected_net
            )

            # Project forward
            projected_sales = calculate_projected_amount(corrected_net, growth_rate)
            calculated_value = calculate_projected_tax(
                projected_sales, corrected_blended_rate
            )
            calculation_details = {
                "corrected_net_taxable": float(corrected_net),
                "corrected_tax": float(corrected_tax),
                "corrected_blended_rate": float(corrected_blended_rate),
                "projected_sales": float(projected_sales),
            }

        # ========== ZIP/City-based calculations ==========

        elif operation == "net_taxable_by_zip":
            zip_code = input.verifier.verifier_values["zip_code"]
            all_orders = taxjar_state.get("orders", [])
            all_refunds = taxjar_state.get("refunds", [])

            # Apply date filtering first
            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
                all_refunds = filter_transactions_by_date_range(
                    all_refunds, from_date, to_date
                )

            # Filter by ZIP
            zip_orders = filter_transactions_by_zip(all_orders, zip_code)
            zip_refunds = filter_transactions_by_zip(all_refunds, zip_code)

            calculated_value = calculate_net_amount(
                zip_orders, zip_refunds, include_shipping=False
            )
            calculation_details = {
                "zip_code": zip_code,
                "order_count": len(zip_orders),
                "refund_count": len(zip_refunds),
            }

        elif operation == "net_taxable_by_city":
            city_name = input.verifier.verifier_values["city_name"]
            all_orders = taxjar_state.get("orders", [])
            all_refunds = taxjar_state.get("refunds", [])

            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
                all_refunds = filter_transactions_by_date_range(
                    all_refunds, from_date, to_date
                )

            city_orders = filter_transactions_by_city(all_orders, city_name)
            city_refunds = filter_transactions_by_city(all_refunds, city_name)

            calculated_value = calculate_net_amount(
                city_orders, city_refunds, include_shipping=False
            )
            calculation_details = {
                "city_name": city_name,
                "order_count": len(city_orders),
                "refund_count": len(city_refunds),
            }

        elif operation == "tax_collected_by_zip":
            zip_code = input.verifier.verifier_values["zip_code"]
            all_orders = taxjar_state.get("orders", [])

            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )

            zip_orders = filter_transactions_by_zip(all_orders, zip_code)
            calculated_value = sum_transaction_sales_tax(zip_orders)
            calculation_details = {
                "zip_code": zip_code,
                "order_count": len(zip_orders),
            }

        elif operation == "tax_collected_by_city":
            city_name = input.verifier.verifier_values["city_name"]
            all_orders = taxjar_state.get("orders", [])

            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )

            city_orders = filter_transactions_by_city(all_orders, city_name)
            calculated_value = sum_transaction_sales_tax(city_orders)
            calculation_details = {
                "city_name": city_name,
                "order_count": len(city_orders),
            }

        elif operation == "net_tax_by_zip":
            zip_code = input.verifier.verifier_values["zip_code"]
            all_orders = taxjar_state.get("orders", [])
            all_refunds = taxjar_state.get("refunds", [])

            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
                all_refunds = filter_transactions_by_date_range(
                    all_refunds, from_date, to_date
                )

            zip_orders = filter_transactions_by_zip(all_orders, zip_code)
            zip_refunds = filter_transactions_by_zip(all_refunds, zip_code)

            calculated_value = calculate_net_sales_tax(zip_orders, zip_refunds)
            calculation_details = {
                "zip_code": zip_code,
                "tax_collected": float(sum_transaction_sales_tax(zip_orders)),
                "tax_refunded": float(sum_transaction_sales_tax(zip_refunds)),
            }

        elif operation == "net_tax_by_city":
            city_name = input.verifier.verifier_values["city_name"]
            all_orders = taxjar_state.get("orders", [])
            all_refunds = taxjar_state.get("refunds", [])

            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
                all_refunds = filter_transactions_by_date_range(
                    all_refunds, from_date, to_date
                )

            city_orders = filter_transactions_by_city(all_orders, city_name)
            city_refunds = filter_transactions_by_city(all_refunds, city_name)

            calculated_value = calculate_net_sales_tax(city_orders, city_refunds)
            calculation_details = {
                "city_name": city_name,
                "tax_collected": float(sum_transaction_sales_tax(city_orders)),
                "tax_refunded": float(sum_transaction_sales_tax(city_refunds)),
            }

        # ========== Rate correction calculations ==========

        elif operation == "corrected_tax_with_rate":
            # Recalculate tax for a specific ZIP using corrected rate
            zip_code = input.verifier.verifier_values["zip_code"]
            corrected_rate = Decimal(
                str(input.verifier.verifier_values["corrected_rate"])
            )
            all_orders = taxjar_state.get("orders", [])
            all_refunds = taxjar_state.get("refunds", [])

            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
                all_refunds = filter_transactions_by_date_range(
                    all_refunds, from_date, to_date
                )

            zip_orders = filter_transactions_by_zip(all_orders, zip_code)
            zip_refunds = filter_transactions_by_zip(all_refunds, zip_code)

            # Calculate net taxable amount for this ZIP
            net_taxable = calculate_net_amount(
                zip_orders, zip_refunds, include_shipping=False
            )

            # Calculate corrected tax
            calculated_value = net_taxable * corrected_rate
            calculation_details = {
                "zip_code": zip_code,
                "net_taxable": float(net_taxable),
                "corrected_rate": float(corrected_rate),
            }

        elif operation == "additional_tax_to_remit":
            # Difference between corrected tax and originally collected
            zip_code = input.verifier.verifier_values["zip_code"]
            corrected_rate = Decimal(
                str(input.verifier.verifier_values["corrected_rate"])
            )
            all_orders = taxjar_state.get("orders", [])
            all_refunds = taxjar_state.get("refunds", [])

            from_date = input.verifier.verifier_values.get("from_date")
            to_date = input.verifier.verifier_values.get("to_date")
            quarter = input.verifier.verifier_values.get("quarter")
            year = input.verifier.verifier_values.get("year", 2025)
            if quarter:
                from_date, to_date = get_quarter_date_range(year, int(quarter))
            if from_date or to_date:
                all_orders = filter_transactions_by_date_range(
                    all_orders, from_date, to_date
                )
                all_refunds = filter_transactions_by_date_range(
                    all_refunds, from_date, to_date
                )

            zip_orders = filter_transactions_by_zip(all_orders, zip_code)
            zip_refunds = filter_transactions_by_zip(all_refunds, zip_code)

            # Net taxable for this ZIP
            net_taxable = calculate_net_amount(
                zip_orders, zip_refunds, include_shipping=False
            )

            # Corrected tax
            corrected_tax = net_taxable * corrected_rate

            # Original tax collected minus refunded
            original_net_tax = calculate_net_sales_tax(zip_orders, zip_refunds)

            # Additional to remit = corrected - original
            calculated_value = corrected_tax - original_net_tax
            calculation_details = {
                "zip_code": zip_code,
                "corrected_tax": float(corrected_tax),
                "original_net_tax": float(original_net_tax),
            }

        elif operation == "total_corrected_liability":
            # This would need a list of ZIP corrections - for now, use basic calculation
            # In practice, you'd pass in a corrections map
            calculated_value = calculate_net_sales_tax(orders, refunds)
            calculation_details = {
                "note": "Use individual ZIP corrections to build total",
            }

        # ====================
        # TAX CALCULATION VERIFICATION
        # ====================

        elif operation == "tax_calc_match":
            # Compare recorded sales_tax against calculated
            expected_rate = Decimal(
                str(input.verifier.verifier_values["expected_rate"])
            )
            total_expected = Decimal("0")
            total_recorded = Decimal("0")
            for order in orders:
                amount = Decimal(str(order.get("amount", 0)))
                recorded_tax = Decimal(str(order.get("sales_tax", 0)))
                expected_tax = amount * expected_rate
                total_expected += expected_tax
                total_recorded += recorded_tax
            calculated_value = total_recorded  # Return recorded for comparison
            calculation_details = {
                "total_expected_tax": float(total_expected),
                "total_recorded_tax": float(total_recorded),
                "discrepancy": float(total_expected - total_recorded),
            }

        elif operation == "tax_discrepancy_total":
            # Sum of all discrepancies
            expected_rate = Decimal(
                str(input.verifier.verifier_values["expected_rate"])
            )
            total_discrepancy = Decimal("0")
            for order in orders:
                amount = Decimal(str(order.get("amount", 0)))
                recorded_tax = Decimal(str(order.get("sales_tax", 0)))
                expected_tax = amount * expected_rate
                total_discrepancy += expected_tax - recorded_tax
            calculated_value = total_discrepancy
            calculation_details = {
                "expected_rate": float(expected_rate),
                "order_count": len(orders),
            }

        # ====================
        # JURISDICTION BREAKDOWN
        # ====================

        elif operation == "state_tax_component":
            # Sum state_tax_collectable from tax calculations (in taxes data or order breakdown)
            calculated_value = aggregate_field(orders, "state_tax_collectable", "sum")
            source_used = "orders"
            # Also check taxes data if orders don't have breakdown
            taxes = taxjar_state.get("taxes", [])
            if calculated_value == 0 and taxes:
                calculated_value = aggregate_field(
                    taxes, "state_tax_collectable", "sum"
                )
                source_used = "taxes"
            calculation_details = {"source": source_used}

        elif operation == "county_tax_component":
            calculated_value = aggregate_field(orders, "county_tax_collectable", "sum")
            source_used = "orders"
            taxes = taxjar_state.get("taxes", [])
            if calculated_value == 0 and taxes:
                calculated_value = aggregate_field(
                    taxes, "county_tax_collectable", "sum"
                )
                source_used = "taxes"
            calculation_details = {"source": source_used}

        elif operation == "city_tax_component":
            calculated_value = aggregate_field(orders, "city_tax_collectable", "sum")
            source_used = "orders"
            taxes = taxjar_state.get("taxes", [])
            if calculated_value == 0 and taxes:
                calculated_value = aggregate_field(taxes, "city_tax_collectable", "sum")
                source_used = "taxes"
            calculation_details = {"source": source_used}

        elif operation == "special_district_tax_component":
            calculated_value = aggregate_field(
                orders, "special_district_tax_collectable", "sum"
            )
            source_used = "orders"
            taxes = taxjar_state.get("taxes", [])
            if calculated_value == 0 and taxes:
                calculated_value = aggregate_field(
                    taxes, "special_district_tax_collectable", "sum"
                )
                source_used = "taxes"
            calculation_details = {"source": source_used}

        # ====================
        # ECONOMIC NEXUS CALCULATIONS
        # ====================

        elif operation == "nexus_sales_check":
            # Check if sales to state exceed nexus threshold
            state_code = input.verifier.verifier_values["state_code"]
            threshold = Decimal(str(input.verifier.verifier_values["threshold"]))
            state_orders = filter_transactions_by_state(orders, state_code)
            total_sales = sum_transaction_amounts(state_orders, include_shipping=False)
            calculated_value = total_sales
            exceeds_threshold = total_sales >= threshold
            calculation_details = {
                "state": state_code,
                "threshold": float(threshold),
                "exceeds_threshold": exceeds_threshold,
                "order_count": len(state_orders),
            }

        elif operation == "nexus_transaction_check":
            # Check if transaction count to state exceeds nexus threshold
            state_code = input.verifier.verifier_values["state_code"]
            threshold = int(input.verifier.verifier_values["threshold"])
            state_orders = filter_transactions_by_state(orders, state_code)
            transaction_count = len(state_orders)
            calculated_value = Decimal(str(transaction_count))
            exceeds_threshold = transaction_count >= threshold
            calculation_details = {
                "state": state_code,
                "threshold": threshold,
                "exceeds_threshold": exceeds_threshold,
            }

        # ====================
        # FILING CALCULATIONS
        # ====================

        elif operation == "filing_period_sales":
            # Total taxable sales for filing period
            calculated_value = calculate_net_amount(
                orders, refunds, include_shipping=False
            )
            calculation_details = {
                "order_count": len(orders),
                "refund_count": len(refunds),
            }

        elif operation == "filing_period_tax_due":
            # Net tax due for filing period
            calculated_value = calculate_net_sales_tax(orders, refunds)
            calculation_details = {
                "tax_collected": float(sum_transaction_sales_tax(orders)),
                "tax_refunded": float(sum_transaction_sales_tax(refunds)),
            }

        elif operation == "filing_states_count":
            # Count unique states with tax obligations
            states = set()
            for order in orders:
                state = order.get("to_state")
                if state:
                    states.add(state.upper())
            calculated_value = Decimal(str(len(states)))
            calculation_details = {"states": list(states)}

        # ====================
        # RATE VALIDATION
        # ====================

        elif operation == "avg_effective_rate":
            # Average effective rate across orders
            total_tax = sum_transaction_sales_tax(orders)
            total_amount = sum_transaction_amounts(orders, include_shipping=False)
            calculated_value = calculate_blended_rate(total_tax, total_amount)
            calculation_details = {
                "total_tax": float(total_tax),
                "total_amount": float(total_amount),
            }

        elif operation == "rate_variance":
            # Variance from expected rate
            expected_rate = Decimal(
                str(input.verifier.verifier_values["expected_rate"])
            )
            total_tax = sum_transaction_sales_tax(orders)
            total_amount = sum_transaction_amounts(orders, include_shipping=False)
            effective_rate = calculate_blended_rate(total_tax, total_amount)
            calculated_value = effective_rate - expected_rate
            calculation_details = {
                "effective_rate": float(effective_rate),
                "expected_rate": float(expected_rate),
            }

        else:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Unknown operation: {operation}",
            )

        logger.info(
            f"TaxJar calculation: {calculation_type} = {calculated_value} "
            f"(expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error calculating {calculation_type}: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Calculation failed: {str(e)}",
        )

    # Compare results
    result_type = calc_config.get("result_type", "decimal")

    if result_type == "percentage":
        # For percentage comparisons, use percentage tolerance
        variance = abs(calculated_value - expected_value)
        pct_tolerance = Decimal("0.001")  # 0.1% tolerance for rates
        within_tolerance = variance <= pct_tolerance
        message = (
            f"{calculation_type}: {float(calculated_value) * 100:.2f}% "
            f"{'✓' if within_tolerance else f'✗ expected {float(expected_value) * 100:.2f}%'}"
        )
    else:
        # For monetary values
        variance = abs(calculated_value - expected_value)
        within_tolerance = variance <= tolerance
        message = (
            f"{calculation_type}: ${float(calculated_value):,.2f} "
            f"{'✓' if within_tolerance else f'✗ expected ${float(expected_value):,.2f}'}"
        )

    result = VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if within_tolerance else 0.0,
        verifier_result_values={
            "calculated_value": float(calculated_value),
            "expected_value": float(expected_value),
            "variance": float(variance),
            "within_tolerance": within_tolerance,
            "calculation_type": calculation_type,
            **calculation_details,
        },
        message=message,
    )

    # Log success
    if within_tolerance:
        logger.info(
            f"✓ Verifier {input.verifier.verifier_id} passed: {calculation_type} matched within tolerance"
        )
    else:
        logger.warning(
            f"✗ Verifier {input.verifier.verifier_id} failed: {calculation_type} "
            f"variance ${float(variance)} exceeds tolerance ${float(tolerance)}"
        )

    return result
