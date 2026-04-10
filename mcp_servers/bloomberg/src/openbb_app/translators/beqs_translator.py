import logging
from datetime import date
from typing import Any

from fastapi_app.models.base import SecurityResponseError
from fastapi_app.models.beqs import (
    BeqsOverrides,
    BeqsRequest,
    BeqsResponse,
    BeqsSecurityInfo,
)
from openbb_app.adapters.sector_mapper import SectorMapper
from openbb_app.providers.equity import EquityProvider

logger = logging.getLogger(__name__)


class BeqsTranslator:
    """Translates between Bloomberg BEQS format and OpenBB."""

    def __init__(self, equity_provider: EquityProvider):
        self.provider = equity_provider

    def _get_override_value(self, overrides: BeqsOverrides | dict, key: str) -> Any | None:
        """
        Safely retrieve an attribute value from the overrides object,
        handling both dataclass (BeqsOverrides) and dictionary inputs.
        This fixes the AttributeError.
        """
        if isinstance(overrides, dict):
            return overrides.get(key)
        # If it's a dataclass object (BeqsOverrides) or other object
        return getattr(overrides, key, None)

    def _build_screen_filters(self, overrides: BeqsOverrides) -> dict[str, Any]:
        """Convert BEQS overrides to OpenBB screen parameters."""
        filters = {}

        filters = {}

        def get_val(key: str) -> Any:
            return self._get_override_value(overrides, key)

        # Market cap filters
        market_cap_min = get_val("marketCapMin")
        if market_cap_min is not None:
            filters["mktcap_min"] = market_cap_min

        market_cap_max = get_val("marketCapMax")
        if market_cap_max is not None:
            filters["mktcap_max"] = market_cap_max

        # Valuation filters
        pe_ratio_min = get_val("peRatioMin")
        if pe_ratio_min is not None:
            filters["pe_min"] = pe_ratio_min

        pe_ratio_max = get_val("peRatioMax")
        if pe_ratio_max is not None:
            filters["pe_max"] = pe_ratio_max

        # Income filters
        dividend_yield_min = get_val("dividendYieldMin")
        if dividend_yield_min is not None:
            filters["div_yield_min"] = dividend_yield_min

        dividend_yield_max = get_val("dividendYieldMax")
        if dividend_yield_max is not None:
            filters["div_yield_max"] = dividend_yield_max

        # Sector/Industry (provider-specific mapping needed)
        sector_val = get_val("sector")
        if sector_val:
            # Use the dedicated mapper class to translate the sector value
            provider_sector = SectorMapper.map_to_provider(sector_val)
            filters["sector"] = provider_sector

        industry_val = get_val("industry")
        if industry_val:
            filters["industry"] = str(industry_val)

        return filters

    async def execute(self, request: BeqsRequest) -> BeqsResponse:
        """Execute BEQS screen and return formatted response."""
        logger.info(f"Translating BEQS request: {request.screenName}")

        try:
            # Build filters from overrides
            filters = self._build_screen_filters(request.overrides)

            # Execute screen via provider
            results = await self.provider.screen_equity(filters)

            # Format results as BeqsSecurityInfo
            securities = []
            for _, result in enumerate(results):
                sec_info = BeqsSecurityInfo(
                    security=f"{result.get('symbol', 'UNKNOWN')} US Equity",
                    ticker=result.get("symbol", ""),
                    name=result.get("name", ""),
                    exchange=result.get("exchange", "US"),
                    marketSector="Equity",
                    industry=result.get("industry", ""),
                    customFields={
                        "marketCap": result.get("market_cap"),
                        "peRatio": result.get("pe_ratio"),
                        "dividendYield": result.get("dividend_yield"),
                    },
                )
                securities.append(sec_info)

            today_date_str = date.today().strftime("%Y%m%d")
            as_of_date = self._get_override_value(request.overrides, "asOfDate") or today_date_str
            return BeqsResponse(
                screenName=request.screenName,
                screenType=request.screenType,
                asOfDate=as_of_date,
                totalSecurities=len(securities),
                securities=securities,
                responseErrors=[],
            )

        except Exception as e:
            logger.error(f"BEQS translation error: {e}")
            return BeqsResponse(
                screenName=request.screenName,
                screenType=request.screenType,
                asOfDate="",
                totalSecurities=0,
                securities=[],
                responseErrors=[
                    SecurityResponseError(
                        security=request.screenName,
                        errorCode="SCREEN_EXECUTION_ERROR",
                        message=str(e),
                    )
                ],
            )
