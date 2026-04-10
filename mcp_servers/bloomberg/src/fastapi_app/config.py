"""FastAPI Configuration."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """FastAPI application settings."""

    model_config = SettingsConfigDict(env_prefix="")

    app_name: str = "BLPAPI Emulator"
    app_version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 8000

    # Data Mode Configuration
    mode: str = "online"  # Options: "online" or "offline"

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Validate that MODE is either 'online' or 'offline'."""
        allowed_modes = {"online", "offline"}
        mode_lower = v.lower()
        if mode_lower not in allowed_modes:
            raise ValueError(
                f"Invalid MODE '{v}'. Must be one of: {', '.join(sorted(allowed_modes))}"
            )
        return mode_lower

    # Testing
    mock_openbb: bool = False

    # OpenBB Adapter Configuration
    openbb_max_concurrent: int = 10
    openbb_timeout_seconds: int = 30

    # Offline Mode Configuration
    duckdb_path: str = "data/offline.duckdb"


settings = Settings()
