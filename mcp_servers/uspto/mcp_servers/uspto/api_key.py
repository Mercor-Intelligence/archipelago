"""API key accessor - delegates to config.settings."""

from __future__ import annotations

from mcp_servers.uspto.config import configure, get_settings


def set_api_key(key: str | None) -> None:
    """Store the USPTO API key."""
    configure(api_key=key)


def get_api_key() -> str | None:
    """Return the stored API key."""
    return get_settings().api_key
