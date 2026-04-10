"""TaxJar domain-specific verifiers.

Verifiers for grading TaxJar tax calculation and compliance tasks:

## Structured Verifiers (Dropdown-based UI)
- Field Check: Verify order/refund counts, nexus regions, field values, etc.
- Calculation: Verify merchandise sales, tax liability calculations
- Tax Projection: Verify projected values with growth rates
- ZIP/City Filtering: Filter by to_zip or to_city columns
- Rate Corrections: Verify tax recalculations with corrected rates

## Generic Verifiers (Maximum Flexibility)
- Generic Field: Access any field on any record with path notation
- Expression: Custom formulas with variable aggregation
- Multi-State: Verify calculations across multiple states/ZIPs
- Customer Exemption: Verify exemption certificates and orders
"""

from .calculation import taxjar_calculation_eval
from .field_check import taxjar_field_check_eval
from .generic import (
    # Utility functions for use in other verifiers
    aggregate_field,
    evaluate_expression,
    find_records_matching,
    get_nested_field,
    taxjar_customer_exemption_eval,
    taxjar_expression_eval,
    taxjar_generic_field_eval,
    taxjar_multi_state_eval,
)
from .utils import (
    filter_transactions_by_city,
    filter_transactions_by_date_range,
    filter_transactions_by_state,
    filter_transactions_by_zip,
    get_rate_for_zip,
    get_transaction_field,
    sum_transaction_amounts,
    sum_transaction_sales_tax,
)

__all__ = [
    # Structured verifiers
    "taxjar_field_check_eval",
    "taxjar_calculation_eval",
    # Generic verifiers
    "taxjar_generic_field_eval",
    "taxjar_expression_eval",
    "taxjar_multi_state_eval",
    "taxjar_customer_exemption_eval",
    # Generic utilities
    "get_nested_field",
    "find_records_matching",
    "aggregate_field",
    "evaluate_expression",
    # Filtering utilities
    "filter_transactions_by_city",
    "filter_transactions_by_date_range",
    "filter_transactions_by_state",
    "filter_transactions_by_zip",
    "get_rate_for_zip",
    "get_transaction_field",
    "sum_transaction_amounts",
    "sum_transaction_sales_tax",
]
