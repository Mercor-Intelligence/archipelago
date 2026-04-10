from dataclasses import dataclass
from typing import Any, ClassVar

from fastapi_app.models.base import BaseRequest, BaseResponse

INTRADAY_BAR_REQUEST = "IntradayBarRequest"
SUPPORTED_INTERVALS = [1, 5, 15, 30, 60]
SUPPORTED_EVENT_TYPES = ["TRADE"]
DEFAULT_EVENT_TYPE = "TRADE"


@dataclass
class IntradayBarRequest(BaseRequest):
    """Request model for intraday bar data"""

    INTERVAL_MAP: ClassVar[dict[int, str]] = {
        1: "1m",
        5: "5m",
        15: "15m",
        30: "30m",
        60: "1h",
    }

    security: str
    interval: int
    startDateTime: str
    endDateTime: str
    eventType: str | None = DEFAULT_EVENT_TYPE
    responseFormat: str | None = "JSON"

    @property
    def mapped_interval(self) -> str:
        """Return the string representation of the interval."""
        return self.INTERVAL_MAP.get(self.interval, f"{self.interval}m")


@dataclass
class BarTickData:
    """Single bar tick data"""

    time: str
    PX_OPEN: float | None = None
    PX_HIGH: float | None = None
    PX_LOW: float | None = None
    PX_LAST: float | None = None
    VOLUME: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary"""
        return {
            "time": self.time,
            "PX_OPEN": self.PX_OPEN,
            "PX_HIGH": self.PX_HIGH,
            "PX_LOW": self.PX_LOW,
            "PX_LAST": self.PX_LAST,
            "VOLUME": self.VOLUME,
        }


@dataclass
class IntradayBarResponse(BaseResponse):
    """Response model for intraday bar data"""

    security: str
    eventType: str
    interval: int
    barData: dict[str, Any]
    sequenceNumber: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "security": self.security,
            "eventType": self.eventType,
            "interval": self.interval,
            "barData": self.barData,
        }
        if self.sequenceNumber is not None:
            result["sequenceNumber"] = self.sequenceNumber
        return result
