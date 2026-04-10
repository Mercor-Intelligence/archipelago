from dataclasses import dataclass
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """SSE event types - used across all response types"""

    RESPONSE = "RESPONSE"
    PARTIAL_RESPONSE = "PARTIAL_RESPONSE"
    ERROR = "ERROR"


@dataclass
class BaseRequest:
    """
    Base request model - all request types inherit from this
    """

    requestType: str


@dataclass
class BaseResponse:
    """
    Base response model - all response types should implement to_dict()
    """

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary - must be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement to_dict()")


@dataclass
class ResponseEnvelope:
    """
    Generic response envelope for SSE or single JSON
    Works with any response type (IntradayBar, ReferenceData, HistoricalData, etc.)
    """

    eventType: EventType
    response: BaseResponse

    def to_dict(self) -> dict[str, Any]:
        """
        Convert to dictionary
        The response dict will have the key matching the response class name
        e.g., IntradayBarResponse -> {"IntradayBarResponse": {...}}
        """
        response_class_name = self.response.__class__.__name__
        return {"eventType": self.eventType.value, response_class_name: self.response.to_dict()}


@dataclass
class ErrorResponse:
    """Error response model - generic across all endpoints"""

    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary"""
        return {"error": {"code": self.code, "message": self.message}}


@dataclass
class ValidationError(Exception):
    """Validation error model - used by all validators"""

    code: str
    message: str

    def __str__(self):
        return f"{self.code}: {self.message}"


@dataclass
class SecurityResponseError(BaseResponse):
    """Represents an error encountered while processing a single security in the screen."""

    security: str
    errorCode: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "security": self.security,
            "errorCode": self.errorCode,
            "message": self.message,
        }
