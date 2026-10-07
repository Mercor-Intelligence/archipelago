"""Configuration module for FMP MCP Server with offline mode support.

This module provides:
- Mode enum (ONLINE/OFFLINE) for server operation mode
- Config class using pydantic-settings for environment variable loading
- Mode detection with priority: FMP_OFFLINE_MODE > --offline flag > API key presence
"""

import sys
from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

# Calculate server directory from this file's location (mcp_servers/fmp_server/config.py)
_SERVER_DIR = Path(__file__).parent
_DEFAULT_DB_PATH = _SERVER_DIR / "data" / "fmp.db"


class Mode(str, Enum):
    """Server operation mode."""

    ONLINE = "online"
    OFFLINE = "offline"


class Config(BaseSettings):
    """FMP MCP Server configuration.

    Mode detection priority (highest to lowest):
    1. FMP_OFFLINE_MODE=true environment variable
    2. --offline CLI flag
    3. FMP_API_KEY not set → offline mode
    4. FMP_API_KEY set → online mode
    """

    # API Configuration
    fmp_api_key: str | None = Field(
        default=None,
        description="FMP API key for online mode",
    )
    fmp_api_base_url: str = Field(
        default="https://financialmodelingprep.com/stable",
        description="FMP API base URL",
    )

    # Mode Configuration
    fmp_offline_mode: bool = Field(
        default=False,
        description="Force offline mode via environment variable",
    )

    # Database Configuration
    fmp_database_url: str = Field(
        default=f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}",
        description="SQLite database URL for offline mode",
    )

    # Logging Configuration
    fmp_log_level: str = Field(
        default="INFO",
        description="Logging verbosity level",
    )

    # Pagination Configuration
    default_list_limit: int = Field(
        default=50,
        alias="APP_FMP_LIST_DEFAULT_LIMIT",
        description="Default pagination limit",
    )
    max_list_limit: int = Field(
        default=100,
        alias="APP_FMP_LIST_MAX_LIMIT",
        description="Maximum pagination limit",
    )

    model_config = {
        "env_prefix": "",  # No prefix - use exact env var names
        "env_file": ".env",  # Load .env file from current working directory
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }

    @property
    def mode(self) -> Mode:
        """Determine the current server mode.

        Priority (highest to lowest):
        1. FMP_OFFLINE_MODE=true → Offline
        2. --offline CLI flag → Offline
        3. FMP_API_KEY not set → Offline
        4. FMP_API_KEY set → Online
        """
        # Priority 1: Environment variable explicitly set
        if self.fmp_offline_mode:
            return Mode.OFFLINE

        # Priority 2: CLI flag
        if "--offline" in sys.argv:
            return Mode.OFFLINE

        # Priority 3 & 4: Based on API key presence
        if not self.fmp_api_key:
            return Mode.OFFLINE

        return Mode.ONLINE

    @property
    def is_offline_mode(self) -> bool:
        """Check if server is running in offline mode."""
        return self.mode == Mode.OFFLINE

    @property
    def is_online_mode(self) -> bool:
        """Check if server is running in online mode."""
        return self.mode == Mode.ONLINE

    def apply_limit(self, limit: int | None) -> int:
        """Apply default limit if None, cap at max_list_limit."""
        if limit is None:
            return self.default_list_limit
        return min(limit, self.max_list_limit)


@lru_cache
def get_config() -> Config:
    """Get the singleton config instance.

    Uses lru_cache to ensure only one Config instance is created.
    This is important because Config reads from environment variables
    which should be consistent throughout the application lifecycle.
    """
    return Config()


# Convenience exports for backward compatibility
config = get_config()
