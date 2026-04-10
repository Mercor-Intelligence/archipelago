from enum import Enum

from pydantic import BaseModel


class ServiceStatus(str, Enum):
    """Service health status"""

    HEALTHY = "healthy"
    FAILED = "failed"
    INITIALIZING = "initializing"


class ProviderStatus(BaseModel):
    """Individual provider status"""

    name: str
    enabled: bool
    has_credentials: bool
    error: str | None = None


class OpenBBServiceHealth(BaseModel):
    """Health check response"""

    status: ServiceStatus
    providers: list[ProviderStatus]
    error: str | None = None


class InitializationError(Exception):
    """Initalization error response"""

    def __init__(self, message: str, error: str = ""):  # type: ignore
        self.message = message
        self.error = error

        full_message = f"{message}\n"

        super().__init__(full_message)

    def __str__(self):
        return self.message
