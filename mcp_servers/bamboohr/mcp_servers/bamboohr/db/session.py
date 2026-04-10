"""Database session management.

Provides async SQLAlchemy session for database operations with configurable
database path for both persistent (file) and in-memory (testing) modes.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import StaticPool, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

# Database configuration
# Default to in-memory SQLite for reproducibility in AI training/evaluation
# Override via DATABASE_URL or BAMBOOHR_DB_PATH env var for persistence
# e.g., DATABASE_URL=sqlite+aiosqlite:///./data.db or BAMBOOHR_DB_PATH=/path/to/data.db
_bamboohr_db_path = os.environ.get("BAMBOOHR_DB_PATH")
if _bamboohr_db_path:
    DATABASE_URL = f"sqlite+aiosqlite:///{_bamboohr_db_path}"
else:
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

# Check if using in-memory mode
IN_MEMORY = ":memory:" in DATABASE_URL

if IN_MEMORY:
    # StaticPool ensures all connections share the same in-memory database
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )


def _set_sqlite_pragma(dbapi_conn, connection_record):
    """Enable foreign key constraints for SQLite connections."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Enable foreign key constraints on each connection (required for SQLite)
event.listen(engine.sync_engine, "connect", _set_sqlite_pragma)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Simple flag to track if database has been initialized (for lazy init in production)
_db_initialized = False
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    """Get or create the init lock (lazy creation for event loop compatibility)."""
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def init_db():
    """Initialize database (create tables) and seed system data.

    Automatically seeds field definitions required for metadata tools
    (get_fields, update_field_options) to work correctly.
    """
    global _db_initialized
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed system data (field definitions) for blank-slate functionality
    # Late import to avoid circular dependency
    from .seed import seed_system_data

    async with AsyncSessionLocal() as session:
        await seed_system_data(session)
        logger.debug("[init_db] Seeded system data (field definitions)")

    _db_initialized = True


async def reset_db():
    """Reset database by dropping and recreating all tables.

    Used by the reset_state tool. After reset, automatically seeds
    system data (field definitions) for blank-slate functionality.
    """
    global _db_initialized
    async with _get_init_lock():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # Seed system data (field definitions) for blank-slate functionality
        # Late import to avoid circular dependency
        from .seed import seed_system_data

        async with AsyncSessionLocal() as session:
            await seed_system_data(session)
            logger.debug("[reset_db] Seeded system data (field definitions)")

        _db_initialized = True


@asynccontextmanager
async def get_session():
    """Get database session.

    Automatically initializes the database on first use if not already initialized.
    This enables lazy initialization for production without requiring explicit
    init_db() calls at startup.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(MyModel))
    """
    global _db_initialized
    # Lazy initialization for production
    if not _db_initialized:
        async with _get_init_lock():
            # Double-check after acquiring lock
            if not _db_initialized:
                await init_db()

    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
