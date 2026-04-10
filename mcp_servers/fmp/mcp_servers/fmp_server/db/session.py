"""Async database session management for FMP MCP Server.

This module provides:
- Async SQLAlchemy engine creation with aiosqlite
- Async session factory for database operations
- Database initialization (table creation)
- Connection lifecycle management
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Support all execution methods:
# 1. python main.py (direct execution from server dir)
# 2. python -m mcp_servers.fmp_server (module execution)
try:
    from ..config import get_config
except ImportError:
    # Direct execution - db is a top-level package with no parent
    from config import get_config

from .models import Base

# Module-level engine instance (singleton pattern)
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Get or create the async SQLAlchemy engine.

    Creates a singleton engine instance using the database URL from config.
    The engine uses aiosqlite for async SQLite operations.

    Returns:
        AsyncEngine: The SQLAlchemy async engine instance.

    Raises:
        RuntimeError: If engine creation fails.
    """
    global _engine

    if _engine is None:
        config = get_config()
        database_url = config.fmp_database_url

        # Ensure the data directory exists for SQLite
        if database_url.startswith("sqlite"):
            # Extract path from URL like 'sqlite+aiosqlite:///./data/fmp.db'
            db_path = database_url.split("///")[-1]
            if db_path and not db_path.startswith(":memory:"):
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        _engine = create_async_engine(
            database_url,
            echo=config.fmp_log_level.upper() == "DEBUG",
            # SQLite-specific settings
            connect_args={"check_same_thread": False} if "sqlite" in database_url else {},
        )

    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory.

    Creates a singleton session factory bound to the engine.

    Returns:
        async_sessionmaker: Factory for creating async sessions.
    """
    global _session_factory

    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session as a context manager.

    Provides a session that automatically handles commit/rollback
    and cleanup on exit.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(Company))
            companies = result.scalars().all()

    Yields:
        AsyncSession: An async SQLAlchemy session.

    Raises:
        Exception: Re-raises any exception after rolling back.
    """
    factory = _get_session_factory()
    session = factory()

    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Initialize the database by creating all tables.

    Creates all tables defined in the models if they don't exist.
    Safe to call multiple times (idempotent).

    Usage:
        await init_db()
    """
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_engine() -> None:
    """Close the database engine and release connections.

    Should be called during application shutdown to cleanly
    release database resources.

    Usage:
        await close_engine()
    """
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def drop_all_tables() -> None:
    """Drop all tables from the database.

    WARNING: This is destructive and should only be used for testing
    or database reset scenarios.

    Usage:
        await drop_all_tables()
    """
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
