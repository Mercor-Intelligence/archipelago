"""Database session management for Workday HCM.

Provides both synchronous and asynchronous SQLAlchemy sessions for database operations.
"""

import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

# Database configuration
# Default to file-based SQLite in db/ directory for persistence across sessions
# Can be overridden via WORKDAY_DB_PATH environment variable
# Set WORKDAY_DB_PATH=:memory: for in-memory testing
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "workday_hcm.db")
DATABASE_PATH = os.environ.get("WORKDAY_DB_PATH", DEFAULT_DB_PATH)

# Global variables (lazily initialized)
_engine = None
_async_engine = None
_session_maker = None
_async_session_maker = None
_db_initialized = False


def get_database_url():
    """Get database URL from environment or default."""
    # Check WORKDAY_DB_PATH first (for import script compatibility)
    if DATABASE_PATH != DEFAULT_DB_PATH:
        return f"sqlite:///{DATABASE_PATH}"
    return os.getenv("WORKDAY_DATABASE_URL") or os.getenv(
        "WORKDAY_DB_URL", "sqlite:///workday_hcm.db"
    )


def _extract_sqlite_path(db_url: str) -> str | None:
    """Extract file path from SQLite URL.

    Returns None if not a SQLite file-based database.
    """
    # Remove async driver prefix if present
    url = db_url.replace("+aiosqlite", "")

    if not url.startswith("sqlite:///"):
        return None

    # sqlite:/// = relative path, sqlite://// = absolute path
    path = url[len("sqlite:///") :]

    # Skip in-memory databases
    if not path or path == ":memory:":
        return None

    return path


def _ensure_database_exists():
    """Ensure the database file and directory exist for SQLite.

    If the database file doesn't exist, creates the directory and
    initializes the database schema using SQLAlchemy's create_all().

    This provides a fallback when alembic migrations didn't run during build.
    """
    global _db_initialized

    if _db_initialized:
        return

    db_url = get_database_url()
    db_path = _extract_sqlite_path(db_url)

    if db_path is None:
        # Not a file-based SQLite database
        _db_initialized = True
        return

    path = Path(db_path)

    # Check if database file exists
    if path.exists():
        logger.debug(f"Database file exists: {path}")
        _db_initialized = True
        return

    # Database doesn't exist - create it
    logger.warning(
        f"Database file not found at {path}. "
        "This may indicate alembic migrations didn't run during build. "
        "Creating database with base schema..."
    )

    # Ensure parent directory exists
    parent_dir = path.parent
    if not parent_dir.exists():
        logger.info(f"Creating database directory: {parent_dir}")
        parent_dir.mkdir(parents=True, exist_ok=True)

    # Create database with schema
    # Note: This uses SQLAlchemy's create_all() which creates tables
    # but doesn't run alembic migrations (no seed data, etc.)
    sync_url = db_url.replace("+aiosqlite", "")
    temp_engine = create_engine(sync_url, echo=False)

    try:
        Base.metadata.create_all(temp_engine)
        logger.info(f"Database initialized at {path}")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise
    finally:
        temp_engine.dispose()

    _db_initialized = True


def get_engine():
    """Get or create synchronous database engine (lazy initialization)."""
    global _engine
    if _engine is None:
        # Ensure database file exists for SQLite (creates if missing)
        _ensure_database_exists()

        # Convert async URL to sync if needed
        db_url = get_database_url().replace("+aiosqlite", "")
        engine_kwargs = {"echo": False}

        # SQLite race condition prevention: Use SERIALIZABLE isolation level.
        # This provides the strongest isolation guarantee, ensuring that
        # concurrent transactions behave as if they were executed serially.
        # Note: with_for_update() is ignored by SQLite, so this is required.
        if "sqlite" in db_url:
            engine_kwargs["isolation_level"] = "SERIALIZABLE"

        _engine = create_engine(db_url, **engine_kwargs)

        # Configure SQLite-specific settings
        if "sqlite" in db_url:

            @event.listens_for(_engine, "connect")
            def set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_async_engine():
    """Get or create async database engine (lazy initialization)."""
    global _async_engine
    if _async_engine is None:
        # Ensure database file exists for SQLite (creates if missing)
        _ensure_database_exists()

        db_url = get_database_url()
        # Ensure async driver for SQLite
        if "sqlite:///" in db_url and "aiosqlite" not in db_url:
            db_url = db_url.replace("sqlite:///", "sqlite+aiosqlite:///")
        engine_kwargs = {"echo": False}

        # SQLite race condition prevention: Use SERIALIZABLE isolation level.
        # See get_engine() for detailed explanation of why this is required.
        if "sqlite" in db_url:
            engine_kwargs["isolation_level"] = "SERIALIZABLE"

        _async_engine = create_async_engine(db_url, **engine_kwargs)

        # Configure SQLite-specific settings
        if "sqlite" in db_url:

            @event.listens_for(_async_engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _async_engine


# Expose async engine for REST bridge compatibility (uses lazy getter pattern)
# This avoids the module-level call that defeats lazy initialization
class _EngineLazyGetter:
    """Lazy getter for async engine to support REST bridge without breaking lazy init."""

    def __getattr__(self, name):
        """Delegate all attribute access to the lazily-initialized async engine."""
        return getattr(get_async_engine(), name)


engine = _EngineLazyGetter()


def get_session_maker():
    """Get or create synchronous session maker (lazy initialization)."""
    global _session_maker
    if _session_maker is None:
        _session_maker = sessionmaker(get_engine(), class_=Session, expire_on_commit=False)
    return _session_maker


def get_async_session_maker():
    """Get or create async session maker (lazy initialization)."""
    global _async_session_maker
    if _async_session_maker is None:
        _async_session_maker = sessionmaker(
            get_async_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _async_session_maker


def init_db():
    """Initialize database (create tables).

    Note: In production, use Alembic migrations instead.
    This is only for testing/development convenience.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)


@contextmanager
def get_session():
    """Get synchronous database session.

    Usage:
        with get_session() as session:
            result = session.execute(select(Worker))
            session.commit()  # Or rollback on error
    """
    session_maker = get_session_maker()
    session = session_maker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@asynccontextmanager
async def get_async_session():
    """Get async database session.

    Usage:
        async with get_async_session() as session:
            result = await session.execute(select(Worker))
            await session.commit()  # Or rollback on error
    """
    session_maker = get_async_session_maker()
    session = session_maker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def reset_engine():
    """Reset engine and session maker (used for testing)."""
    global _engine, _async_engine, _session_maker, _async_session_maker, _db_initialized
    _engine = None
    _async_engine = None
    _session_maker = None
    _async_session_maker = None
    _db_initialized = False


def is_sqlite(session: Session) -> bool:
    """Check if the session is using SQLite database."""
    bind = session.get_bind()
    return "sqlite" in str(bind.url)


def acquire_write_lock(session: Session) -> None:
    """Acquire write lock for databases that need explicit locking.

    For SQLite: No-op. The engine is configured with isolation_level="SERIALIZABLE"
    at creation time, which ensures transactions behave as if executed serially.
    This prevents TOCTOU race conditions where two transactions could both
    SELECT a position as "open" before either writes.

    For other databases: No-op, as they use row-level locking via SELECT FOR UPDATE.

    Note: This function is kept for API compatibility. The actual locking is
    handled at the engine level (SQLite) or via with_for_update() (PostgreSQL/MySQL).
    """
    # SQLite: isolation_level="SERIALIZABLE" is set at engine creation time
    # PostgreSQL/MySQL: Use SELECT FOR UPDATE in repository methods
    pass
