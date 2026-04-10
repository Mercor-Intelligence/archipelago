"""Health check schema."""

from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class HealthCheckRequest(BaseModel):
    """No parameters."""


class ServerInfoRequest(BaseModel):
    """No parameters."""


class HealthCheckResponse(BaseModel):
    status: str = Field(..., description="healthy/unhealthy")
    version: str
    mode: str
    persistence: str
    timestamp: str
    database_connection: str = Field(..., description="connected/disconnected")
    uptime_seconds: float = Field(..., description="Server uptime in seconds")
    table_counts: dict[str, int]
    metrics: dict[str, Any] = Field(
        ..., description="Request metrics including total requests, errors, and per-tool statistics"
    )


class ServerInfoResponse(BaseModel):
    name: str = Field(..., description="Service name")
    status: str = Field(..., description="Service health status")
    features: dict[str, str | bool] = Field(
        ...,
        description="Key service capabilities (e.g., authentication, authorization, etc.)",
    )
