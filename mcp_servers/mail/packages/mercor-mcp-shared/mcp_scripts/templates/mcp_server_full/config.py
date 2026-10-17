"""Configuration management for __SNAKE_NAME__.

Uses pydantic-settings to load from environment variables or .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class __PASCAL_NAME__Settings(BaseSettings):
    """Application settings loaded from environment.

    TODO: Add your configuration here.

    Example:
        from pydantic import Field

        api_key: str = Field(..., description="API key")
        database_url: str = Field("sqlite+aiosqlite:///./data.db")
        debug: bool = Field(False, description="Debug mode")
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Global settings instance (loads on import)
settings = __PASCAL_NAME__Settings()
