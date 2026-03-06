from enum import Enum
from functools import cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(Enum):
    LOCAL = "local"
    DEV = "dev"
    DEMO = "demo"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: Environment = Environment.LOCAL

    SAVE_WEBHOOK_URL: str | None = None
    SAVE_WEBHOOK_API_KEY: str | None = None
    SCORE_WEBHOOK_URL: str | None = None

    # Datadog
    DATADOG_LOGGING: bool = False
    DATADOG_API_KEY: str | None = None
    DATADOG_APP_KEY: str | None = None

    # LiteLLM Proxy
    # If set, all LLM requests will be routed through the proxy
    LITELLM_PROXY_API_BASE: str | None = None
    LITELLM_PROXY_API_KEY: str | None = None

    # Scraping / web content (used by ACE link verification)
    ACE_FIRECRAWL_API_KEY: str | None = None

    # Data Delivery API (document parsing with caching)
    MERCOR_DELIVERY_API_KEY: str | None = None


@cache
def get_settings() -> Settings:
    return Settings()
