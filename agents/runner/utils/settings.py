from enum import Enum
from functools import cache

from pydantic_settings import BaseSettings


class Environment(Enum):
    LOCAL = "local"
    DEV = "dev"
    DEMO = "demo"
    PROD = "prod"


class Settings(BaseSettings):
    ENV: Environment = Environment.LOCAL

    # Agent execution hard timeout
    AGENT_TIMEOUT_SECONDS: int = 12 * 60 * 60  # 12 hours

    # RL Studio API
    RL_STUDIO_API: str | None = None
    RL_STUDIO_API_KEY: str | None = None

    # Webhook for saving results
    SAVE_WEBHOOK_URL: str | None = None
    SAVE_WEBHOOK_API_KEY: str | None = None

    # Postgres logging
    POSTGRES_LOGGING: bool = False
    POSTGRES_URL: str | None = None

    # Redis logging
    REDIS_LOGGING: bool = False
    REDIS_HOST: str | None = None
    REDIS_PORT: int | None = None
    REDIS_USER: str | None = None
    REDIS_PASSWORD: str | None = None
    REDIS_STREAM_PREFIX: str = "trajectory_logs"

    # Datadog logging
    DATADOG_LOGGING: bool = False
    DATADOG_API_KEY: str | None = None
    DATADOG_APP_KEY: str | None = None

    # File logging
    FILE_LOGGING: bool = False
    FILE_LOG_PATH: str | None = None

    # LiteLLM Proxy
    # If set, all LLM requests will be routed through the proxy
    LITELLM_PROXY_API_BASE: str | None = None
    LITELLM_PROXY_API_KEY: str | None = None

    # Scraping / web content
    ACE_FIRECRAWL_API_KEY: str | None = None
    ACE_SEARCHAPI_API_KEY: str | None = None  # YouTube transcript API
    ACE_REDDIT_PROXY: str | None = None  # Proxy for Reddit requests


@cache
def get_settings() -> Settings:
    return Settings()
