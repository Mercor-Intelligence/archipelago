import redis.asyncio as redis

from runner.utils.settings import get_settings

settings = get_settings()
REDIS_HOST = settings.REDIS_HOST
REDIS_PORT = settings.REDIS_PORT
REDIS_USER = settings.REDIS_USER
REDIS_PASSWORD = settings.REDIS_PASSWORD

if not REDIS_HOST or not REDIS_PORT or not REDIS_USER or not REDIS_PASSWORD:
    raise ValueError("Redis configuration is not set")

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    username=REDIS_USER,
)
