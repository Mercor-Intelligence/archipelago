"""Connection management for USPTO offline database.

Provides both synchronous (for data ingestion) and asynchronous (for client queries)
connection management following the session database pattern.
"""

import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import aiosqlite

from mcp_servers.uspto.config import get_settings
from mcp_servers.uspto.offline.db.init_db import init_database, migrate_schema, verify_schema

_db_path: str | None = None


def _ensure_pragmas(conn: sqlite3.Connection) -> None:
    """Enforce SQLite PRAGMAs per connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")


def _init_db_sync() -> None:
    """Synchronous database initialization for lazy loading.

    Used by get_sync_connection() and get_async_connection() when DB is not initialized.

    Raises:
        RuntimeError: If called in online mode (offline DB should not be used)
    """
    global _db_path

    if _db_path is not None:
        return

    # Check if we're in online mode - offline DB should not be initialized
    if get_settings().online_mode:
        raise RuntimeError(
            "Cannot initialize offline database in online mode. "
            "Offline database connections should not be used when online_mode=True."
        )

    # Get database path from settings
    db_path = get_settings().offline_db

    # Ensure database exists and is initialized
    db_file = Path(db_path)
    if not db_file.exists():
        init_database(db_path)
    else:
        # Database exists - verify schema and migrate if needed
        if not verify_schema(db_path):
            # Schema is incomplete or outdated - run migrations
            migrate_schema(db_path)

    _db_path = db_path


async def init_db() -> None:
    """Initialize the offline database if not already initialized.

    Uses USPTO_OFFLINE_DB from settings (env var or default ./data/uspto_offline.db).

    Note: No lock needed - this is called once at startup via asyncio.run() in CLI context.
    """
    _init_db_sync()


async def cleanup_db() -> None:
    """Clean up database resources."""
    global _db_path

    _db_path = None


def current_db_path() -> str | None:
    """Return the current database path."""
    return _db_path


@contextmanager
def get_sync_connection():
    """Get a synchronous database connection for data ingestion.

    Lazily initializes the database if not already initialized.

    Yields:
        sqlite3.Connection: Database connection with autocommit disabled

    Example:
        with get_sync_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO patents ...")
            conn.commit()
    """
    # Lazy initialization for REST bridge imports
    if _db_path is None:
        _init_db_sync()

    conn = sqlite3.connect(_db_path)
    _ensure_pragmas(conn)

    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def get_async_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Get an asynchronous database connection for client queries.

    Lazily initializes the database if not already initialized.

    Yields:
        aiosqlite.Connection: Async database connection

    Example:
        async with get_async_connection() as conn:
            async with conn.execute("SELECT * FROM patents WHERE ...") as cursor:
                rows = await cursor.fetchall()
    """
    # Lazy initialization for REST bridge imports
    if _db_path is None:
        _init_db_sync()

    async with aiosqlite.connect(_db_path) as conn:
        # Set pragmas
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA synchronous=NORMAL")

        # Enable Row factory for dict-like access
        conn.row_factory = aiosqlite.Row

        try:
            yield conn
        except Exception:
            await conn.rollback()
            raise


@contextmanager
def transaction():
    """Context manager for database transactions (sync).

    Automatically commits on success, rolls back on error.

    Example:
        with transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO patents ...")
            cursor.execute("INSERT INTO inventors ...")
            # Auto-commits here if no exception
    """
    with get_sync_connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
