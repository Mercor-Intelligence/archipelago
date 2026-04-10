"""Shared models used across all Bloomberg request types."""

from .base import BaseRequest, BaseResponse, ErrorResponse, EventType, ResponseEnvelope
from .error_models import (
    ErrorCategory,
    ErrorInfo,
    FieldException,
    ResponseError,
    SecurityError,
    create_field_exception,
    create_response_error,
    create_security_error,
)
from .field_registry import FieldDefinition, SupportLevel, field_registry

__all__ = [
    # Base models
    "BaseRequest",
    "BaseResponse",
    "ResponseEnvelope",
    "EventType",
    "ErrorResponse",
    # Field registry
    "field_registry",
    "FieldDefinition",
    "SupportLevel",
    # Error models
    "ErrorCategory",
    "FieldException",
    "ErrorInfo",
    "SecurityError",
    "ResponseError",
    "create_field_exception",
    "create_security_error",
    "create_response_error",
]
