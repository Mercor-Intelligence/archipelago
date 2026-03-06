"""Application settings management using pydantic-settings.

This module defines application configuration settings that can be loaded
from environment variables. All settings use pydantic-settings for validation
and type safety.
"""

from enum import Enum
from functools import cache

from pydantic_settings import BaseSettings


class Environment(Enum):
    LOCAL = "local"
    DEV = "dev"
    DEMO = "demo"
    PROD = "prod"


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All settings can be overridden via environment variables. Settings are
    validated using Pydantic and cached after first access.
    """

    # Environment
    ENV: Environment = Environment.LOCAL

    # Datadog logging
    DATADOG_LOGGING: bool = False
    DATADOG_API_KEY: str | None = None
    DATADOG_APP_KEY: str | None = None

    # S3 Configuration
    S3_SNAPSHOTS_BUCKET: str = "snapshots"
    """S3 bucket name for storing snapshots. Required for snapshot operations."""

    S3_SNAPSHOTS_PREFIX: str = ""
    """Optional prefix for snapshot objects in S3 bucket (e.g., 'snapshots/')."""

    S3_DEFAULT_REGION: str = "us-west-2"
    """Default AWS region for S3 operations (e.g., 'us-west-2')."""

    # S3 Credentials (for S3-compatible API access)
    S3_ACCESS_KEY_ID: str | None = None
    """AWS access key ID for S3 authentication. If not set, uses default credential chain."""

    S3_SECRET_ACCESS_KEY: str | None = None
    """AWS secret access key for S3 authentication. Required if S3_ACCESS_KEY_ID is set."""

    S3_SESSION_TOKEN: str | None = None
    """AWS session token for temporary credentials (optional)."""

    # Subsystem names for data extraction
    FILESYSTEM_SUBSYSTEM_NAME: str = "filesystem"
    """Name of the filesystem subsystem root directory."""

    APPS_DATA_SUBSYSTEM_NAME: str = ".apps_data"
    """Name of the apps data subsystem root directory."""


@cache
def get_settings() -> Settings:
    """Get cached application settings instance.

    Settings are loaded from environment variables on first call and cached
    for subsequent calls. This ensures consistent settings across the application
    and avoids repeated environment variable lookups.

    Returns:
        Settings instance with values from environment variables
    """
    return Settings()
