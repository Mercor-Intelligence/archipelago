from dataclasses import dataclass, field
from typing import Any

from fastapi_app.models.base import (
    BaseRequest,
    BaseResponse,
    SecurityResponseError,
)
from fastapi_app.models.enums import Industry, ScreenType, Sector

BEQS_RESPONSE = "BeqsResponse"


@dataclass
class BeqsOverrides:
    """
    Nested model representing optional overrides for the equity screen parameters.
    Fields with "enum" types are typed as Union[Enum, str] to allow custom values.
    """

    asOfDate: str | None = None
    startDate: str | None = None
    endDate: str | None = None

    sector: Sector | str | None = None
    industry: Industry | str | None = None

    marketCapMin: float | None = None
    marketCapMax: float | None = None
    peRatioMin: float | None = None
    peRatioMax: float | None = None
    dividendYieldMin: float | None = None
    dividendYieldMax: float | None = None

    # TODO: Custom filters allow flexible typing (string|float|enum)
    # customFilter: Any | None = None


BEQS_REQUEST = "BeqsRequest"


@dataclass(kw_only=True)
class BeqsRequest(BaseRequest):
    """Request model for Bloomberg Equity Screening (BEQS) data."""

    screenName: str
    screenType: ScreenType
    group: str

    requestType: str = BEQS_REQUEST
    overrides: BeqsOverrides = field(default_factory=BeqsOverrides)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "screenName": self.screenName,
            "screenType": self.screenType,
            "group": self.group,
            "requestType": self.requestType,
            "overrides": self.overrides,
        }


@dataclass
class BeqsSecurityInfo:
    """Represents a single security returned by the BEQS screen."""

    security: str
    ticker: str
    name: str
    exchange: str
    marketSector: str
    industry: str

    customFields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "security": self.security,
            "ticker": self.ticker,
            "name": self.name,
            "exchange": self.exchange,
            "marketSector": self.marketSector,
            "industry": self.industry,
            "customFields": self.customFields,
        }


@dataclass
class BeqsResponse(BaseResponse):
    """Main response model for Bloomberg Equity Screening (BEQS)."""

    screenName: str
    screenType: ScreenType
    asOfDate: str
    totalSecurities: int
    securities: list[BeqsSecurityInfo]

    responseType: str = BEQS_RESPONSE
    responseErrors: list[SecurityResponseError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the response model and its nested dataclasses to a dictionary.
        This is required by the BaseResponse interface.
        """
        return {
            "responseType": self.responseType,
            "screenName": self.screenName,
            "screenType": self.screenType.value,
            "asOfDate": self.asOfDate,
            "totalSecurities": self.totalSecurities,
            "securities": [s.to_dict() for s in self.securities],
            "responseErrors": [e.to_dict() for e in self.responseErrors],
        }
