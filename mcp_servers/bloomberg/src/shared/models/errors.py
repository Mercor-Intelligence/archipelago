from dataclasses import dataclass
from typing import Any


@dataclass
class ResponseError:
    """Generic response error model used across all endpoints"""

    source: str
    category: str
    message: str
    subcategory: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "source": self.source,
                "category": self.category,
                "message": self.message,
                "subcategory": self.subcategory,
            }
        }


@dataclass
class SecurityError(ResponseError):
    """Error specific to a security"""


@dataclass
class FieldError(ResponseError):
    """Field validation error"""

    field: str | None = None


@dataclass
class ValidationError(ResponseError):
    """Validation error for request payloads or fields"""
