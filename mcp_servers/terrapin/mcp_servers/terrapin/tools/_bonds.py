"""Bond data tools with offline mode support.

From Terrapin API:

Government and corporate bonds (6 endpoints):
- search bonds
- bond reference data
- bond pricing history
- bond pricing latest
- bond cashflows
- inflation factors

US municipal bonds (7 endpoints):
- search municipal bonds
- muni reference data
- muni pricing latest
- muni pricing history
- muni pricing daily bulk
- calculate muni yield from price
- muni cashflows

When offline mode is active, all queries use local fixture data.
When online, queries use the Terrapin Finance API.
"""

import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from models import (  # noqa: E402
    BondCashflowResult,
    BondCashflowsRequest,
    BondCashflowsResponse,
    BondPricingHistoryRequest,
    BondPricingHistoryResponse,
    BondPricingLatestRequest,
    BondPricingLatestResponse,
    BondPricingResult,
    BondReferenceDataRequest,
    BondReferenceDataResponse,
    BondReferenceResult,
    BondSearchResult,
    InflationFactorResult,
    InflationFactorsRequest,
    InflationFactorsResponse,
    MuniBondSearchResult,
    MuniCashflowResult,
    MuniCashflowsRequest,
    MuniCashflowsResponse,
    MuniPricingDailyBulkRequest,
    MuniPricingDailyBulkResponse,
    MuniPricingHistoryRequest,
    MuniPricingHistoryResponse,
    MuniPricingLatestRequest,
    MuniPricingLatestResponse,
    MuniPricingResult,
    MuniReferenceDataRequest,
    MuniReferenceDataResponse,
    MuniReferenceResult,
    MuniYieldFromPriceRequest,
    MuniYieldFromPriceResponse,
    MuniYieldResult,
    SearchBondsRequest,
    SearchBondsResponse,
    SearchMunicipalBondsRequest,
    SearchMunicipalBondsResponse,
)
from utils.adapters import (
    adapt_bond_cashflows,
    adapt_bond_pricing,
    adapt_bond_reference,
    adapt_bond_search,
    adapt_inflation_factors,
    adapt_muni_cashflows,
    adapt_muni_pricing,
    adapt_muni_reference,
    adapt_muni_search,
    adapt_muni_yield,
)
from utils.api_client import get_api_client, is_offline_mode
from utils.decorators import make_async_background, with_retry
from utils.exceptions import NonRetryableError
from utils.fixtures import (
    fixture_calculate_muni_yield_from_price,
    fixture_get_bond_cashflows,
    fixture_get_bond_pricing_history,
    fixture_get_bond_pricing_latest,
    fixture_get_bond_reference_data,
    fixture_get_inflation_factors,
    fixture_get_muni_cashflows,
    fixture_get_muni_pricing_daily_bulk,
    fixture_get_muni_pricing_history,
    fixture_get_muni_pricing_latest,
    fixture_get_muni_reference_data,
    fixture_search_bonds,
    fixture_search_municipal_bonds,
)
from utils.validation import validate_isin, validate_isins


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def search_bonds(request: SearchBondsRequest) -> SearchBondsResponse:
    """Search government/corporate bonds by country, coupon, maturity, currency, rating.

    Use to find bonds.
    """
    if is_offline_mode():
        results = fixture_search_bonds(
            countries=request.countries,
            coupon_min=request.coupon_min,
            coupon_max=request.coupon_max,
            maturity_date_min=request.maturity_date_min,
            maturity_date_max=request.maturity_date_max,
            currencies=request.currencies,
            issue_rating_group=request.issue_rating_group,
            limit=request.limit if request.limit is not None else 100,
        )
        adapted = adapt_bond_search(results)
        return SearchBondsResponse(bonds=[BondSearchResult(**r) for r in adapted])

    data = {}
    if request.countries:
        data["country_codes"] = request.countries
    if request.coupon_min is not None:
        data["coupon_min"] = request.coupon_min
    if request.coupon_max is not None:
        data["coupon_max"] = request.coupon_max
    if request.maturity_date_min:
        data["maturity_date_min"] = request.maturity_date_min
    if request.maturity_date_max:
        data["maturity_date_max"] = request.maturity_date_max
    if request.currencies:
        data["currencies"] = request.currencies
    if request.issue_rating_group:
        data["issue_rating_group"] = request.issue_rating_group
    data["limit"] = request.limit if request.limit is not None else 100

    with get_api_client() as client:
        response = client.post("/bond_search", json=data)
        response.raise_for_status()
        api_data = response.json()
        bonds = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return SearchBondsResponse(bonds=[BondSearchResult(**b) for b in bonds])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_bond_reference_data(request: BondReferenceDataRequest) -> BondReferenceDataResponse:
    """Get reference data for bonds by ISIN. Use for bond metadata.

    For government and corporate bonds only — not for municipal bonds
    (use get_muni_reference_data instead).
    """
    valid_isins, invalid_isins = validate_isins(request.isins)
    if not valid_isins:
        raise NonRetryableError(f"No valid ISINs provided. Invalid ISINs: {invalid_isins}")

    if is_offline_mode():
        results = fixture_get_bond_reference_data(isins=valid_isins)
        adapted = adapt_bond_reference(results)
        reference = [BondReferenceResult(**r) for r in adapted]
        return _build_reference_response(reference, valid_isins, invalid_isins)

    with get_api_client() as client:
        response = client.post("/bond_reference", json={"isins": valid_isins})
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500]
            if 400 <= e.response.status_code < 500:
                raise NonRetryableError(
                    f"Terrapin API error ({e.response.status_code}): {body}"
                ) from None
            raise
        api_data = response.json()
        refs = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        reference = [BondReferenceResult(**r) for r in refs]
        return _build_reference_response(reference, valid_isins, invalid_isins)


def _build_reference_response(
    reference: list[BondReferenceResult],
    requested_isins: list[str],
    invalid_isins: list[str] | None = None,
) -> BondReferenceDataResponse:
    """Build reference response with helpful message for missing ISINs."""
    invalid_isins = invalid_isins or []
    invalid_note = f" Invalid ISIN format (skipped): {invalid_isins}." if invalid_isins else ""

    if not reference:
        return BondReferenceDataResponse(
            reference=[],
            message="No bond reference data found for the requested ISINs. "
            "The ISINs may be invalid, not in our database, or may be municipal bonds "
            f"(use get_muni_reference_data for US municipal bonds).{invalid_note}",
            requested_isins=requested_isins,
        )

    found_isins = {r.isin for r in reference}
    missing_isins = [isin for isin in requested_isins if isin not in found_isins]

    if missing_isins or invalid_isins:
        msg_parts = [
            f"Data found for {len(found_isins)} of {len(requested_isins)} requested ISINs."
        ]
        if missing_isins:
            msg_parts.append("Missing ISINs may be invalid or not in our database.")
        if invalid_isins:
            msg_parts.append(f"Invalid ISIN format (skipped): {invalid_isins}.")
        return BondReferenceDataResponse(
            reference=reference,
            message=" ".join(msg_parts),
            requested_isins=missing_isins if missing_isins else None,
        )

    return BondReferenceDataResponse(reference=reference)


def _map_bond_pricing_fields(record: dict) -> dict:
    """Map API field names to model field names for bond pricing."""
    result = dict(record)
    # API may return 'date' instead of 'pricing_date'
    if "pricing_date" not in result and "date" in result:
        result["pricing_date"] = result.pop("date")
    return result


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_bond_pricing_history(request: BondPricingHistoryRequest) -> BondPricingHistoryResponse:
    """Get historical pricing. Use for prices and time series.

    For government and corporate bonds only — not for municipal bonds
    (use get_muni_pricing_history instead).
    """
    cleaned_isin = validate_isin(request.isin)
    if not cleaned_isin:
        raise NonRetryableError(
            f"Invalid ISIN format: '{request.isin}'. Must be 12 alphanumeric characters."
        )

    if is_offline_mode():
        results = fixture_get_bond_pricing_history(
            isin=cleaned_isin,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        adapted = adapt_bond_pricing(results)
        pricing = [BondPricingResult(**r) for r in adapted]
        return _build_pricing_history_response(pricing, request)

    data = {"isin": cleaned_isin, "start_date": request.start_date, "end_date": request.end_date}

    with get_api_client() as client:
        response = client.post("/bond_pricing_history", json=data)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500]
            if 400 <= e.response.status_code < 500:
                raise NonRetryableError(
                    f"Terrapin API error ({e.response.status_code}): {body}"
                ) from None
            raise
        api_data = response.json()
        pricing_data = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        pricing_data = [_map_bond_pricing_fields(p) for p in pricing_data]
        pricing = [BondPricingResult(**p) for p in pricing_data]
        return _build_pricing_history_response(pricing, request)


def _build_pricing_history_response(
    pricing: list[BondPricingResult], request: BondPricingHistoryRequest
) -> BondPricingHistoryResponse:
    """Build pricing history response with helpful message when no data found."""
    if not pricing:
        return BondPricingHistoryResponse(
            pricing=[],
            message=f"No pricing history found for ISIN '{request.isin}' between "
            f"{request.start_date} and {request.end_date}. "
            f"This could mean: (1) the ISIN is invalid or not in our database, "
            f"(2) no pricing data exists for this date range, or "
            f"(3) this may be a municipal bond (use get_muni_pricing_history instead).",
        )

    return BondPricingHistoryResponse(pricing=pricing)


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_bond_pricing_latest(request: BondPricingLatestRequest) -> BondPricingLatestResponse:
    """Get latest pricing. Use for prices and time series.

    For government and corporate bonds only — not for municipal bonds
    (use get_muni_pricing_latest instead).
    """
    valid_isins, invalid_isins = validate_isins(request.isins)
    if not valid_isins:
        raise NonRetryableError(f"No valid ISINs provided. Invalid ISINs: {invalid_isins}")

    invalid_note = f"Invalid ISIN format (skipped): {invalid_isins}." if invalid_isins else None

    if is_offline_mode():
        results = fixture_get_bond_pricing_latest(
            isins=valid_isins,
            as_of_date=request.as_of_date,
        )
        adapted = adapt_bond_pricing(results)
        return BondPricingLatestResponse(
            pricing=[BondPricingResult(**r) for r in adapted],
            message=invalid_note,
        )

    data: dict[str, object] = {"isins": valid_isins}
    if request.as_of_date:
        data["as_of_date"] = request.as_of_date

    with get_api_client() as client:
        response = client.post("/bond_pricing_latest", json=data)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500]
            if 400 <= e.response.status_code < 500:
                raise NonRetryableError(
                    f"Terrapin API error ({e.response.status_code}): {body}"
                ) from None
            raise
        api_data = response.json()
        pricing = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        pricing = [_map_bond_pricing_fields(p) for p in pricing]
        return BondPricingLatestResponse(
            pricing=[BondPricingResult(**p) for p in pricing],
            message=invalid_note,
        )


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_bond_cashflows(request: BondCashflowsRequest) -> BondCashflowsResponse:
    """Get cash flow schedules. Use for yield and cash flow analysis.

    For government and corporate bonds only — not for municipal bonds
    (use get_muni_cashflows instead).
    """
    if is_offline_mode():
        results = fixture_get_bond_cashflows(isins=request.isins)
        adapted = adapt_bond_cashflows(results)
        return BondCashflowsResponse(cashflows=[BondCashflowResult(**r) for r in adapted])

    with get_api_client() as client:
        response = client.post("/bond_cashflows", json={"isins": request.isins})
        response.raise_for_status()
        api_data = response.json()
        cfs = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return BondCashflowsResponse(cashflows=[BondCashflowResult(**c) for c in cfs])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_inflation_factors(request: InflationFactorsRequest) -> InflationFactorsResponse:
    """Get inflation factors. Use for inflation-linked bonds."""
    if is_offline_mode():
        results = fixture_get_inflation_factors(
            country=request.country,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        adapted = adapt_inflation_factors(results)
        return InflationFactorsResponse(factors=[InflationFactorResult(**r) for r in adapted])

    data = {"country": request.country}
    if request.start_date:
        data["start_date"] = request.start_date
    if request.end_date:
        data["end_date"] = request.end_date

    with get_api_client() as client:
        response = client.post("/inflation_factors", json=data)
        response.raise_for_status()
        api_data = response.json()
        factors = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return InflationFactorsResponse(factors=[InflationFactorResult(**f) for f in factors])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def search_municipal_bonds(request: SearchMunicipalBondsRequest) -> SearchMunicipalBondsResponse:
    """Search US municipal bonds from Terrapin Finance."""
    if is_offline_mode():
        results = fixture_search_municipal_bonds(
            states=request.states,
            coupon_min=request.coupon_min,
            coupon_max=request.coupon_max,
            maturity_date_min=request.maturity_date_min,
            maturity_date_max=request.maturity_date_max,
            sectors=request.sectors,
            sources_of_repayment=request.sources_of_repayment,
            is_insured=request.is_insured,
            limit=request.limit if request.limit is not None else 100,
        )
        adapted = adapt_muni_search(results)
        return SearchMunicipalBondsResponse(bonds=[MuniBondSearchResult(**r) for r in adapted])

    data = {}
    if request.states:
        data["states"] = request.states
    if request.coupon_min is not None:
        data["coupon_min"] = request.coupon_min
    if request.coupon_max is not None:
        data["coupon_max"] = request.coupon_max
    if request.maturity_date_min:
        data["maturity_date_min"] = request.maturity_date_min
    if request.maturity_date_max:
        data["maturity_date_max"] = request.maturity_date_max
    if request.sectors:
        data["sectors"] = request.sectors
    if request.sources_of_repayment:
        data["sources_of_repayment"] = request.sources_of_repayment
    if request.is_insured is not None:
        data["is_insured"] = request.is_insured
    data["limit"] = request.limit if request.limit is not None else 100

    with get_api_client() as client:
        response = client.post("/muni_search", json=data)
        response.raise_for_status()
        api_data = response.json()
        bonds = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return SearchMunicipalBondsResponse(bonds=[MuniBondSearchResult(**b) for b in bonds])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_muni_reference_data(request: MuniReferenceDataRequest) -> MuniReferenceDataResponse:
    """Get full US municipal bond reference data from Terrapin Finance."""
    if is_offline_mode():
        results = fixture_get_muni_reference_data(isins=request.isins)
        adapted = adapt_muni_reference(results)
        return MuniReferenceDataResponse(reference=[MuniReferenceResult(**r) for r in adapted])

    with get_api_client() as client:
        response = client.post("/muni_reference", json={"isins": request.isins})
        response.raise_for_status()
        api_data = response.json()
        refs = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return MuniReferenceDataResponse(reference=[MuniReferenceResult(**r) for r in refs])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_muni_pricing_latest(request: MuniPricingLatestRequest) -> MuniPricingLatestResponse:
    """Get latest US municipal bond pricing data from Terrapin Finance."""
    if is_offline_mode():
        results = fixture_get_muni_pricing_latest(
            isins=request.isins,
            as_of_date=request.as_of_date,
        )
        adapted = adapt_muni_pricing(results)
        return MuniPricingLatestResponse(pricing=[MuniPricingResult(**r) for r in adapted])

    data = {"isins": request.isins}
    if request.as_of_date:
        data["as_of_date"] = request.as_of_date

    with get_api_client() as client:
        response = client.post("/muni_pricing_latest", json=data)
        response.raise_for_status()
        api_data = response.json()
        pricing = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return MuniPricingLatestResponse(pricing=[MuniPricingResult(**p) for p in pricing])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_muni_pricing_history(request: MuniPricingHistoryRequest) -> MuniPricingHistoryResponse:
    """Get historical US municipal bond pricing data from Terrapin Finance."""
    if is_offline_mode():
        results = fixture_get_muni_pricing_history(
            isin=request.isin,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        adapted = adapt_muni_pricing(results)
        return MuniPricingHistoryResponse(pricing=[MuniPricingResult(**r) for r in adapted])

    data = {"isin": request.isin, "start_date": request.start_date, "end_date": request.end_date}

    with get_api_client() as client:
        response = client.post("/muni_pricing_history", json=data)
        response.raise_for_status()
        api_data = response.json()
        pricing = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return MuniPricingHistoryResponse(pricing=[MuniPricingResult(**p) for p in pricing])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_muni_pricing_daily_bulk(
    request: MuniPricingDailyBulkRequest,
) -> MuniPricingDailyBulkResponse:
    """Get bulk municipal bond pricing for all ISINs on a specific day."""
    if is_offline_mode():
        results = fixture_get_muni_pricing_daily_bulk(trade_date=request.trade_date)
        adapted = adapt_muni_pricing(results)
        return MuniPricingDailyBulkResponse(pricing=[MuniPricingResult(**r) for r in adapted])

    with get_api_client() as client:
        response = client.post(
            "/muni_pricing_daily_history", json={"trade_date": request.trade_date}
        )
        response.raise_for_status()
        api_data = response.json()
        pricing = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return MuniPricingDailyBulkResponse(pricing=[MuniPricingResult(**p) for p in pricing])


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def calculate_muni_yield_from_price(
    request: MuniYieldFromPriceRequest,
) -> MuniYieldFromPriceResponse:
    """Calculate yield metrics for municipal securities based on price."""
    if is_offline_mode():
        result = fixture_calculate_muni_yield_from_price(
            isin=request.isin,
            price=request.price,
            settlement_date=request.settlement_date,
        )
        adapted = adapt_muni_yield(result)
        if adapted:
            return MuniYieldFromPriceResponse(result=MuniYieldResult(**adapted))
        return MuniYieldFromPriceResponse(result=None)

    calc_data = [
        {
            "isin": request.isin,
            "trade_data": [{"price": request.price, "settlement_date": request.settlement_date}],
        }
    ]

    with get_api_client() as client:
        response = client.post("/muni_price_to_yield", json=calc_data)
        response.raise_for_status()
        api_data = response.json()
        # API returns list of results, get first one
        if isinstance(api_data, list) and len(api_data) > 0:
            return MuniYieldFromPriceResponse(result=MuniYieldResult(**api_data[0]))
        if isinstance(api_data, dict):
            return MuniYieldFromPriceResponse(result=MuniYieldResult(**api_data))
        return MuniYieldFromPriceResponse(result=None)


@with_retry(max_retries=3, base_backoff=1.5)
@make_async_background
def get_muni_cashflows(request: MuniCashflowsRequest) -> MuniCashflowsResponse:
    """Retrieve cash flow schedules for US municipal bonds from Terrapin Finance."""
    if is_offline_mode():
        results = fixture_get_muni_cashflows(isins=request.isins)
        adapted = adapt_muni_cashflows(results)
        return MuniCashflowsResponse(cashflows=[MuniCashflowResult(**r) for r in adapted])

    with get_api_client() as client:
        response = client.post("/muni_cashflows", json={"isins": request.isins})
        response.raise_for_status()
        api_data = response.json()
        cfs = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        return MuniCashflowsResponse(cashflows=[MuniCashflowResult(**c) for c in cfs])
