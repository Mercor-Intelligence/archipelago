"""Database session management for Greenhouse MCP Server.

Provides async SQLAlchemy session for database operations with configurable
database path for both persistent (file) and in-memory (testing) modes.

Database configuration:
- Production: Set GREENHOUSE_DB_PATH for the database file location
  - Example: GREENHOUSE_DB_PATH=/data/greenhouse.db
- Testing: Set GREENHOUSE_DB_PATH=:memory: for in-memory database
- Default: File-based SQLite in db/ directory for persistence across sessions

Only GREENHOUSE_DB_PATH is respected. The generic DATABASE_URL env var is
intentionally ignored to prevent conflicts when multiple MCP servers run in
the same environment.
"""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from db.models import Base
from sqlalchemy import StaticPool, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

# Track if database has been initialized per event loop
# This avoids "bound to a different event loop" errors when event loops change
# (e.g., pytest-asyncio with per-test loops, reloaders, embedded runners)
_db_initialized: dict[int, bool] = {}
# Locks to prevent concurrent initialization, keyed by event loop ID
_init_locks: dict[int, asyncio.Lock] = {}

# Database configuration
# Only GREENHOUSE_DB_PATH is used. DATABASE_URL is deliberately ignored because
# it is a generic variable that other apps (e.g., Workday) or shared
# infrastructure (e.g., mcp_rest_bridge) may set, causing Greenhouse to open
# the wrong database file.
_stale_database_url = os.environ.get("DATABASE_URL")
if _stale_database_url:
    logger.warning(
        "DATABASE_URL is set (%s) but will be ignored. "
        "Greenhouse only uses GREENHOUSE_DB_PATH to avoid cross-app conflicts.",
        _stale_database_url,
    )

if os.environ.get("GREENHOUSE_DB_PATH"):
    DATABASE_PATH = os.environ["GREENHOUSE_DB_PATH"]

    if DATABASE_PATH == ":memory:":
        DATABASE_URL = "sqlite+aiosqlite:///:memory:"
        engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"
        engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            connect_args={"check_same_thread": False},
        )
else:
    # Default: file-based SQLite in db/ directory for persistence across sessions
    DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "greenhouse.db")
    DATABASE_PATH = DEFAULT_DB_PATH
    DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )

logger.info(
    "Greenhouse database: %s (source: %s)",
    DATABASE_PATH,
    "GREENHOUSE_DB_PATH" if os.environ.get("GREENHOUSE_DB_PATH") else "default",
)

# Export IN_MEMORY flag for use by tools (e.g., admin.py snapshot validation)
IN_MEMORY = DATABASE_PATH == ":memory:"

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _enable_foreign_keys(dbapi_conn, connection_record):
    """Enable foreign key constraints for SQLite."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Enable foreign keys for all connections
event.listen(engine.sync_engine, "connect", _enable_foreign_keys)


def _get_init_lock() -> asyncio.Lock:
    """Get or create the initialization lock for the current event loop.

    Creates a lock lazily for each event loop to avoid "bound to a different
    event loop" errors when event loops change (common in pytest-asyncio with
    per-test loops, reloaders, or embedded runners).

    Returns:
        asyncio.Lock bound to the current event loop
    """
    loop = asyncio.get_running_loop()
    loop_id = id(loop)

    if loop_id not in _init_locks:
        _init_locks[loop_id] = asyncio.Lock()

    return _init_locks[loop_id]


def _is_db_initialized() -> bool:
    """Check if database has been initialized for the current event loop.

    Returns:
        True if database is initialized for the current event loop, False otherwise
    """
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    return _db_initialized.get(loop_id, False)


def _set_db_initialized(value: bool) -> None:
    """Set database initialization status for the current event loop.

    Args:
        value: True if database is initialized, False otherwise
    """
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    _db_initialized[loop_id] = value


async def _do_init_db() -> None:
    """Internal function that performs the actual database initialization.

    This function does NOT acquire locks - it should only be called while
    holding the init lock (via _get_init_lock()).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def init_db() -> None:
    """Initialize database by creating all tables.

    This should be called once at server startup.
    Async-safe: uses an async lock to prevent concurrent initialization.
    Event-loop-safe: tracks initialization per event loop.
    """
    async with _get_init_lock():
        if _is_db_initialized():
            return
        await _do_init_db()
        _set_db_initialized(True)


async def _do_drop_tables() -> None:
    """Internal helper to drop all tables (must be called with lock held)."""
    async with engine.begin() as conn:
        # Drop tables individually with `checkfirst` to avoid errors when
        # tables have already been deleted (e.g., reset_state tool invoked).
        for table in reversed(Base.metadata.sorted_tables):
            await conn.run_sync(
                lambda sync_conn, table=table: table.drop(sync_conn, checkfirst=True)
            )


async def drop_db() -> None:
    """Drop all tables from database.

    Use with caution - this destroys all data.
    Primarily used for reset_state tool.
    Async-safe: acquires lock to prevent race with get_session().
    Event-loop-safe: resets initialization flag for current event loop.
    """
    async with _get_init_lock():
        await _do_drop_tables()
        _set_db_initialized(False)


async def reset_db() -> None:
    """Reset database by dropping and recreating all tables.

    This is used by the greenhouse_reset_state tool.
    Async-safe: uses an async lock to prevent race conditions.
    Event-loop-safe: tracks initialization per event loop.
    """
    async with _get_init_lock():
        await _do_drop_tables()
        _set_db_initialized(False)
        # Recreate tables inside the lock to prevent race conditions
        await _do_init_db()
        _set_db_initialized(True)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session as async context manager.

    Lazy initialization: ensures database tables are created on first use.
    Event-loop-safe: tracks initialization per event loop.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()
    """
    # Lazy initialization: ensure database is set up on first use
    if not _is_db_initialized():
        await init_db()

    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency-injection style session getter.

    Alternative to get_session() for use with FastAPI-style dependency injection.
    Includes lazy initialization like get_session().
    Event-loop-safe: tracks initialization per event loop.

    Usage:
        async for session in get_db():
            result = await session.execute(select(User))
    """
    # Lazy initialization: ensure database is set up on first use
    if not _is_db_initialized():
        await init_db()

    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the database engine and close all connections.

    This should be called during shutdown to properly clean up resources.
    Essential for pytest to exit cleanly - without this, the async engine
    keeps background tasks alive that prevent the event loop from closing.
    """
    await engine.dispose()
