import redis.asyncio as redis

from runner.utils.settings import get_settings

settings = get_settings()
REDIS_HOST = settings.REDIS_HOST
REDIS_PORT = settings.REDIS_PORT
REDIS_USER = settings.REDIS_USER
REDIS_PASSWORD = settings.REDIS_PASSWORD

# Importable without Redis: offline deliveries run with REDIS_LOGGING off, and a
# module-level raise makes importing this package crash a trajectory that finished.
redis_client: redis.Redis | None = None

if REDIS_HOST and REDIS_PORT and REDIS_USER and REDIS_PASSWORD:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        username=REDIS_USER,
    )
elif settings.REDIS_LOGGING:
    raise ValueError("REDIS_LOGGING is enabled but Redis configuration is not set")
