"""Configuration management for looker.

Uses pydantic-settings to load from environment variables or .env file.
"""

from loguru import logger
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cache for online mode verification (None = not tested, True/False = result)
_online_mode_verified: bool | None = None
_online_mode_error: str | None = None


class LookerSettings(BaseSettings):
    """Application settings loaded from environment.

    Controls mode (offline/online) and Looker API credentials.

    Mode is auto-detected:
    - If credentials provided → online mode
    - If no credentials → offline mode (fallback)
    - Explicit OFFLINE_MODE setting overrides auto-detection
    """

    # Mode control
    offline_mode: bool | None = Field(
        default=None,
        description="Use mock data (offline) or connect to Looker API (online). "
        "If None (default), auto-detects based on credentials.",
    )

    # Hybrid mode: Use captured real API data in offline mode
    captured_data_file: str | None = Field(
        default=None,
        description="Path to captured API data file for hybrid mode. "
        "If set in offline mode, loads real captured data instead of mock data. "
        "Generate this file using: scripts/looker_online/capture_data.py",
    )
    # Looker API configuration (for online mode)
    looker_base_url: str | None = Field(
        default=None, description="Looker instance URL (e.g., https://company.looker.com:19999)"
    )
    looker_client_id: str | None = Field(default=None, description="Looker API client ID")
    looker_client_secret: str | None = Field(default=None, description="Looker API client secret")
    looker_verify_ssl: bool = Field(default=True, description="Verify SSL certificates")
    looker_timeout: int = Field(default=120, description="API request timeout in seconds")
    looker_sql_create_timeout: int = Field(
        default=10, description="Timeout for SQL query creation (step 1) in seconds"
    )
    looker_sql_run_timeout: int = Field(
        default=30, description="Timeout for SQL query execution (step 2) in seconds"
    )

    # General settings
    debug: bool = Field(default=False, description="Enable debug logging")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def has_credentials(self) -> bool:
        """Check if all required credentials are configured.

        Returns:
            True if base_url, client_id, and client_secret are all set
        """
        return bool(self.looker_base_url and self.looker_client_id and self.looker_client_secret)

    def is_offline_mode(self) -> bool:
        """Determine if we should use offline mode.

        Logic:
        1. If offline_mode is explicitly set (True/False), use that
        2. If credentials exist and are verified working → online mode
        3. If credentials don't exist or failed verification → offline mode

        Returns:
            True for offline mode (mock data), False for online mode (API)
        """
        global _online_mode_verified

        # Explicit setting overrides auto-detection
        if self.offline_mode is not None:
            return self.offline_mode

        # No credentials = offline mode
        if not self.has_credentials():
            return True

        # If we've already verified, use cached result
        if _online_mode_verified is not None:
            return not _online_mode_verified

        # Credentials exist but not yet verified - assume online for now
        # Verification happens on first API call
        return False

    def get_online_error(self) -> str | None:
        """Get the error message if online mode verification failed.

        Returns:
            Error message or None if online mode is working
        """
        return _online_mode_error

    def is_hybrid_mode(self) -> bool:
        """Check if we're in hybrid mode (offline with captured data).

        Returns:
            True if offline mode with captured data file specified
        """
        return self.is_offline_mode() and self.captured_data_file is not None

    def get_mode_description(self) -> str:
        """Get a human-readable description of the current mode.

        Returns:
            String describing the current mode
        """
        base_mode = ""
        if not self.is_offline_mode():
            base_mode = f"ONLINE - Connected to {self.looker_base_url}"
        elif self.is_hybrid_mode():
            base_mode = f"HYBRID - Using captured data from {self.captured_data_file}"
        elif _online_mode_error:
            base_mode = f"OFFLINE - Fallback (online failed: {_online_mode_error})"
        elif self.has_credentials():
            base_mode = "OFFLINE - Credentials present but not yet verified"
        else:
            base_mode = "OFFLINE - Using mock data (no credentials configured)"

        return base_mode


def mark_online_verified(success: bool, error: str | None = None) -> None:
    """Mark online mode as verified (or failed).

    Called by repository after first successful/failed API call.

    Args:
        success: True if online mode is working, False if it failed
        error: Error message if success is False
    """
    global _online_mode_verified, _online_mode_error
    _online_mode_verified = success
    _online_mode_error = error
    if success:
        logger.info(f"Online mode verified - connected to {settings.looker_base_url}")
    else:
        logger.warning(f"Online mode failed, falling back to offline: {error}")


def reset_online_verification() -> None:
    """Reset online mode verification (for testing)."""
    global _online_mode_verified, _online_mode_error
    _online_mode_verified = None
    _online_mode_error = None


# Global settings instance (loads on import)
settings = LookerSettings()
