"""Response models."""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .base import BaseRequest, BaseResponse


class HelloResponse(BaseModel):
    """Hello world response."""

    message: str


@dataclass
class Override:
    """Field override for ReferenceDataRequest."""

    fieldId: str
    value: str


@dataclass
class ReferenceDataRequest(BaseRequest):
    """Reference Data Request model."""

    securities: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    overrides: list[Override] | None = None
    responseFormat: str = "JSON"


@dataclass
class SecurityData:
    """Security data in response."""

    security: str
    sequenceNumber: int
    fieldData: dict[str, Any] = field(default_factory=dict)
    fieldExceptions: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "security": self.security,
            "sequenceNumber": self.sequenceNumber,
            "fieldData": self.fieldData,
        }
        if self.fieldExceptions:
            result["fieldExceptions"] = self.fieldExceptions
        return result


@dataclass
class ReferenceDataResponse(BaseResponse):
    """Reference Data Response container."""

    securityData: list[SecurityData]

    def to_dict(self) -> dict[str, Any]:
        return {"securityData": [sd.to_dict() for sd in self.securityData]}
