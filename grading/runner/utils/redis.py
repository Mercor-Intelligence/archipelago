import redis.asyncio as redis

from runner.utils.settings import get_settings

settings = get_settings()
REDIS_HOST = settings.REDIS_HOST
REDIS_PORT = settings.REDIS_PORT
REDIS_USER = settings.REDIS_USER
REDIS_PASSWORD = settings.REDIS_PASSWORD

# Isolate sequential log writes from the budget pool serving concurrent calls.
REDIS_LOG_MAX_CONNECTIONS = 1
REDIS_BUDGET_MAX_CONNECTIONS = 8
REDIS_POOL_TIMEOUT_SECONDS = 1

if not REDIS_HOST or not REDIS_PORT or not REDIS_USER or not REDIS_PASSWORD:
    raise ValueError("Redis configuration is not set")


def _capped_client(max_connections: int) -> redis.Redis:
    return redis.Redis(
        connection_pool=redis.BlockingConnectionPool(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            username=REDIS_USER,
            max_connections=max_connections,
            timeout=REDIS_POOL_TIMEOUT_SECONDS,
        )
    )


def _build_clients() -> tuple[redis.Redis, redis.Redis]:
    return (
        _capped_client(REDIS_BUDGET_MAX_CONNECTIONS),
        _capped_client(REDIS_LOG_MAX_CONNECTIONS),
    )


redis_client, redis_log_client = _build_clients()
