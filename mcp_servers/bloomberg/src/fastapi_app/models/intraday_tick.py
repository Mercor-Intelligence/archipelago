from dataclasses import dataclass, field
from typing import Any

from fastapi_app.models.base import BaseRequest, BaseResponse

INTRADAY_TICK_REQUEST = "IntradayTickRequest"
INTRADAY_TICK_SUPPORTED_EVENT_TYPES = [
    "TRADE",
    "BID",
    "ASK",
    "BID_BEST",
    "ASK_BEST",
    "BID_YIELD",
    "ASK_YIELD",
    "MID_PRICE",
    "AT_TRADE",
    "BEST_BID",
    "BEST_ASK",
    "SETTLE",
]


@dataclass
class IntradayTickRequest(BaseRequest):
    """Request model for intraday tick data"""

    security: str
    startDateTime: str
    endDateTime: str
    eventTypes: list[str]  # Updated to match the example request
    includeConditionCodes: bool | None = False
    includeExchangeCodes: bool | None = False
    includeBrokerCodes: bool | None = False
    includeRpsCodes: bool | None = False
    includeBicMicCodes: bool | None = False
    includeFunctionCodes: bool | None = False
    includeTradeTime: bool | None = False
    includeSpreadPrice: bool | None = False
    includeYield: bool | None = False
    includeNonTradingDays: bool | None = False
    adjustmentAbnormal: bool | None = False
    adjustmentSplit: bool | None = True
    adjustmentNormal: bool | None = True
    responseFormat: str | None = "JSON"  # Optional field, not in the example request


@dataclass
class EIDData:
    """Represents Bloomberg internal event ID metadata."""

    EID: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, only including non-None fields"""
        result = {}
        if self.EID is not None:
            result["EID"] = self.EID
        if self.description is not None:
            result["description"] = self.description
        return result


@dataclass
class TickData:
    """Represents individual tick data."""

    time: str
    type: str  # Enum: TRADE, BID, ASK, MID_PRICE, BEST_BID, BEST_ASK, etc.
    value: float
    size: int
    conditionCodes: str | None = None
    exchangeCode: str | None = None
    brokerCode: str | None = None
    rpsCode: str | None = None
    bicMicCode: str | None = None
    functionCode: str | None = None
    spreadPrice: float | None = None
    yield_: float | None = field(
        default=None, metadata={"name": "yield"}
    )  # Avoid conflict with Python keyword

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with proper key names (yield_ -> yield)"""
        result = {
            "time": self.time,
            "type": self.type,
            "value": self.value,
            "size": self.size,
        }
        # Only include optional fields if they are not None
        if self.conditionCodes is not None:
            result["conditionCodes"] = self.conditionCodes
        if self.exchangeCode is not None:
            result["exchangeCode"] = self.exchangeCode
        if self.brokerCode is not None:
            result["brokerCode"] = self.brokerCode
        if self.rpsCode is not None:
            result["rpsCode"] = self.rpsCode
        if self.bicMicCode is not None:
            result["bicMicCode"] = self.bicMicCode
        if self.functionCode is not None:
            result["functionCode"] = self.functionCode
        if self.spreadPrice is not None:
            result["spreadPrice"] = self.spreadPrice
        if self.yield_ is not None:
            result["yield"] = self.yield_  # Map yield_ to yield
        return result


@dataclass
class TickDataContainer:
    """Container for tick data and associated metadata."""

    eidData: list[EIDData]
    tickData: list[TickData]


@dataclass
class IntradayTickResponse(BaseResponse):
    """Response model for intraday tick data."""

    tickData: TickDataContainer
    responseError: dict | None = None
    securityError: dict | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tickData": {
                "eidData": [edata.to_dict() for edata in self.tickData.eidData],
                "tickData": [tdata.to_dict() for tdata in self.tickData.tickData],
            }
        }
        if self.responseError is not None:
            result["responseError"] = self.responseError
        if self.securityError is not None:
            result["securityError"] = self.securityError
        if self.message is not None:
            result["message"] = self.message
        return result
