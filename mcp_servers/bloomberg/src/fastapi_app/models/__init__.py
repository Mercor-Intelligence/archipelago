"""Pydantic models for BLPAPI."""

# Base models (for refdata compatibility with main)
# Import from shared models for historical data handler compatibility
from shared.models import (
    ErrorCategory,
    ErrorInfo,
    FieldDefinition,
    FieldException,
    SecurityError,
    SupportLevel,
    create_field_exception,
    create_response_error,
    create_security_error,
    field_registry,
)
from shared.models import (
    ResponseError as SharedResponseError,
)

from .base import (
    BaseRequest,
    BaseResponse,
    ErrorResponse,
    EventType,
    ResponseEnvelope,
    ValidationError,
)

# Data management models (for MCP tools)
from .data_management import (
    DataStatusResponse,
    DataType,
    DateRange,
    DownloadSymbolRequest,
    DownloadSymbolResponse,
    ListSymbolsResponse,
)
from .enums import (
    Industry,
    NonTradingDayFillMethod,
    NonTradingDayFillOption,
    OverrideOption,
    PeriodicityAdjustment,
    PeriodicitySelection,
    PricingOption,
    ScreenType,
    Sector,
)

# Historical Data feature models
from .historical_data import (
    HistoricalDataRequest,
    HistoricalDataResponse,
)

# IntradayBar models
from .intraday_bar import (
    DEFAULT_EVENT_TYPE,
    INTRADAY_BAR_REQUEST,
    SUPPORTED_EVENT_TYPES,
    SUPPORTED_INTERVALS,
    BarTickData,
    IntradayBarRequest,
    IntradayBarResponse,
)
from .intraday_tick import (
    INTRADAY_TICK_REQUEST,
    INTRADAY_TICK_SUPPORTED_EVENT_TYPES,
    EIDData,
    IntradayTickRequest,
    TickData,
)
from .responses import (
    HelloResponse,
    Override,
    ReferenceDataRequest,
    ReferenceDataResponse,
    SecurityData,
)

__all__ = [
    # Base models
    "BaseRequest",
    "BaseResponse",
    "ResponseEnvelope",
    "ErrorResponse",
    "EventType",
    "ValidationError",
    # Historical Data models
    "HistoricalDataRequest",
    "HistoricalDataResponse",
    # IntradayBar models
    "IntradayBarRequest",
    "IntradayBarResponse",
    "BarTickData",
    "INTRADAY_BAR_REQUEST",
    "SUPPORTED_INTERVALS",
    "SUPPORTED_EVENT_TYPES",
    "DEFAULT_EVENT_TYPE",
    # IntradayTick models
    "INTRADAY_TICK_REQUEST",
    "INTRADAY_TICK_SUPPORTED_EVENT_TYPES",
    "IntradayTickRequest",
    "EIDData",
    "TickData",
    "HelloResponse",
    "ReferenceDataRequest",
    "ReferenceDataResponse",
    "SecurityData",
    "Override",
    # Shared models (for historical data compatibility)
    "ErrorCategory",
    "ErrorInfo",
    "FieldDefinition",
    "FieldException",
    "SecurityError",
    "SupportLevel",
    "SharedResponseError",
    "create_field_exception",
    "create_security_error",
    "create_response_error",
    "field_registry",
    # Enums
    "ScreenType",
    "Sector",
    "Industry",
    "PeriodicitySelection",
    "PeriodicityAdjustment",
    "NonTradingDayFillOption",
    "NonTradingDayFillMethod",
    "PricingOption",
    "OverrideOption",
    # Data management models
    "DataType",
    "DateRange",
    "DataStatusResponse",
    "ListSymbolsResponse",
    "DownloadSymbolRequest",
    "DownloadSymbolResponse",
]
