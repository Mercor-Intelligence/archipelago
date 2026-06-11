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

    # RL Studio API (grading config fetch + grading_logs webhook transport)
    RL_STUDIO_API: str | None = None
    RL_STUDIO_API_KEY: str | None = None

    SAVE_WEBHOOK_URL: str | None = None
    SAVE_WEBHOOK_API_KEY: str | None = None
    SCORE_WEBHOOK_URL: str | None = None

    # Postgres logging (grading_logs table; opt-in via grading_log logger)
    POSTGRES_LOGGING: bool = False
    POSTGRES_URL: str | None = None
    # Ship grading logs through the RL Studio API instead of a direct Postgres
    # connection (requires RL_STUDIO_API + RL_STUDIO_API_KEY).
    GRADING_LOGS_VIA_API: bool = True

    # Redis logging (live stream while grading run is active)
    REDIS_LOGGING: bool = False
    REDIS_HOST: str | None = None
    REDIS_PORT: int | None = None
    REDIS_USER: str | None = None
    REDIS_PASSWORD: str | None = None
    REDIS_STREAM_PREFIX: str = "grading_logs"

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


@cache
def get_settings() -> Settings:
    return Settings()
