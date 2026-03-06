"""Pydantic models for MCP gateway endpoints.

This module defines request and response models for the /apps endpoint,
as well as MCP server configuration models.
"""

from typing import Any

from pydantic import BaseModel, Field


class MCPServerConfig(BaseModel):
    """Configuration model for a single MCP server.

    Supports both remote HTTP/SSE servers and local stdio servers.
    """

    transport: str

    # Remote server config
    url: str | None = None
    headers: dict[str, str] | None = None
    auth: Any | None = None

    # Local server config
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None


class MCPSchema(BaseModel):
    """MCP configuration schema.

    Structure: {"mcpServers": {"server_name": MCPServerConfig(...)}}

    The mcpServers value is a dictionary mapping server names to their
    configuration. Each server config is validated against MCPServerConfig
    to ensure it has the correct structure and fields (transport, command,
    args, url, etc.).
    """

    mcpServers: dict[str, MCPServerConfig] = Field(
        ...,
        description="Dictionary mapping server names to their configuration. Can be empty if no servers are configured.",
    )


class AppConfigRequest(MCPSchema):
    """Request to set/update MCP servers configuration.

    This endpoint accepts a full MCP configuration dict and hot-swaps
    the MCP gateway with the new configuration. Inherits validation from
    MCPSchema, ensuring each server config is validated against MCPServerConfig.
    """

    pass


class ServerReadinessResult(BaseModel):
    """Result of checking a single server's readiness.

    Attributes:
        is_ready: Whether the server is ready to handle requests
        message: Success message with tool count, or error description
        attempts: Number of attempts made before success or timeout
        elapsed_seconds: Time taken from start to success or timeout
    """

    is_ready: bool = Field(..., description="Whether the server is ready")
    message: str = Field(..., description="Success message or error description")
    attempts: int = Field(..., description="Number of attempts made")
    elapsed_seconds: float = Field(..., description="Time taken in seconds")


class ServerReadinessDetails(BaseModel):
    """Details about a server's readiness check failure.

    Attributes:
        error: Error message describing why the server failed
        attempts: Number of attempts made before timeout
    """

    error: str = Field(..., description="Error message")
    attempts: int = Field(..., description="Number of attempts made")


class AppConfigResult(BaseModel):
    """Result of setting MCP servers configuration.

    Returned by the /apps endpoint after successfully hot-swapping
    the MCP gateway with new configuration.
    """

    servers: list[str] = Field(
        ...,
        description="List of configured server names",
    )

    duration_ms: float | None = Field(
        default=None,
        description="Total time spent handling the /apps configuration request (includes gateway warmup)",
    )
