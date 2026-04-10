"""Shared middleware components for MCP servers."""

from .injected_errors import (
    ErrorInjectionMiddleware,
    InjectedErrorRule,
    InjectedErrorsConfig,
    InjectedErrorType,
    setup_error_injection,
)
from .latency import LatencyMiddleware
from .logging import LoggingMiddleware
from .ratelimit import Algorithm, RateLimitMiddleware
from .version import __version__

__all__ = [
    "__version__",
    # Error injection
    "ErrorInjectionMiddleware",
    "InjectedErrorRule",
    "InjectedErrorsConfig",
    "InjectedErrorType",
    "setup_error_injection",
    "LatencyMiddleware",
    "LoggingMiddleware",
    "RateLimitMiddleware",
    "Algorithm",
]
