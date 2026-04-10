"""
Terrapin bond tools module.

Re-exports discrete tools for UI generation.
The underscore-prefixed modules (_bonds.py, _meta_tools.py) contain
implementation details not intended for direct import.
"""

from ._bonds import (
    BondCashflowsRequest,
    BondPricingHistoryRequest,
    BondPricingLatestRequest,
    BondReferenceDataRequest,
    InflationFactorsRequest,
    SearchBondsRequest,
    get_bond_cashflows,
    get_bond_pricing_history,
    get_bond_pricing_latest,
    get_bond_reference_data,
    get_inflation_factors,
    search_bonds,
)

__all__ = [
    # Request models
    "SearchBondsRequest",
    "BondReferenceDataRequest",
    "BondPricingLatestRequest",
    "BondPricingHistoryRequest",
    "BondCashflowsRequest",
    "InflationFactorsRequest",
    # Tool functions
    "search_bonds",
    "get_bond_reference_data",
    "get_bond_pricing_latest",
    "get_bond_pricing_history",
    "get_bond_cashflows",
    "get_inflation_factors",
]
