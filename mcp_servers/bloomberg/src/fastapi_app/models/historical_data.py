"""
Historical Data Request and Response Models

Feature-specific models for Bloomberg Historical Data Request.
Follows Bloomberg BLPAPI HistoricalDataRequest format.
"""

from dataclasses import dataclass, field
from typing import Any

from fastapi_app.models.base import BaseRequest, BaseResponse
from fastapi_app.models.responses import SecurityData


@dataclass
class HistoricalDataRequest(BaseRequest):
    """Request model for historical data"""

    securities: list[str]
    fields: list[str]
    start_date: str
    end_date: str
    request_id: str
    periodicity_selection: str = "DAILY"
    adjustment_split: bool = True
    adjustment_normal: bool = True
    adjustment_abnormal: bool = False


@dataclass
class HistoricalDataResponse(BaseResponse):
    """Historical Data Response matching Bloomberg format."""

    securityData: list[SecurityData] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"securityData": [sd.to_dict() for sd in self.securityData]}
