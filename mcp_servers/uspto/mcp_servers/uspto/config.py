"""Configuration management for USPTO MCP Server.

Uses pydantic-settings to consolidate all config in one place.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_NOT_SET: Any = object()


class USPTOSettings(BaseSettings):
    """Application settings loaded from environment or CLI."""

    api_key: str | None = Field(default=None)
    online_mode: bool = Field(default=False)
    offline_db: str = Field(default="./data/uspto_offline.db")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="USPTO_",
        extra="ignore",
    )


settings = USPTOSettings()


def configure(
    api_key: str | None = _NOT_SET,
    online_mode: bool = _NOT_SET,
) -> None:
    """Update settings from CLI args."""
    global settings
    updates = {}
    if api_key is not _NOT_SET:
        updates["api_key"] = api_key
    if online_mode is not _NOT_SET:
        updates["online_mode"] = online_mode
    if updates:
        settings = settings.model_copy(update=updates)


def get_settings() -> USPTOSettings:
    """Get current settings instance."""
    return settings


# Backwards-compat accessors
def set_online_mode(online: bool) -> None:
    """Set online mode."""
    configure(online_mode=online)


def is_online_mode() -> bool:
    """Check if online mode is enabled."""
    return settings.online_mode
