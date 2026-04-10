"""Session-scoped SQLite helpers for the USPTO MCP server."""

from __future__ import annotations

import asyncio
import atexit
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from mcp_servers.uspto.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_db_path: str | None = None
_temp_db_path: Path | None = None
_delete_temp_file: bool = False
_init_lock = asyncio.Lock()
_atexit_registered = False


def _ensure_pragmas(engine: AsyncEngine) -> None:
    """Enforce SQLite PRAGMAs (WAL mode and foreign keys) per connection."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record) -> None:  # pragma: no cover - event hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def _resolve_db_path(raw_path: str | Path | None) -> tuple[str, bool, Path | None]:
    """Return the actual SQLite path, whether to delete it later, and the Path object."""

    if raw_path is None:
        return ":memory:", False, None

    normalized = str(raw_path).strip()
    if not normalized or normalized == ":memory:":
        return ":memory:", False, None

    if normalized.lower() == "temp":
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        temp_file.close()
        path = Path(temp_file.name)
        return path.as_posix(), True, path

    return Path(normalized).as_posix(), False, None


def _sqlite_url(path: str) -> str:
    if path == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    return f"sqlite+aiosqlite:///{Path(path).as_posix()}"


async def _create_schema(engine: AsyncEngine) -> None:
    """Create all database tables for the provided engine."""

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _register_atexit() -> None:
    global _atexit_registered

    if _atexit_registered:
        return

    def _cleanup_on_exit() -> None:
        try:
            asyncio.run(cleanup_db())
        except RuntimeError:
            pass

    atexit.register(_cleanup_on_exit)
    _atexit_registered = True


async def init_db(raw_path: str | Path | None = None) -> None:
    """Initialize the SQLite engine, create tables, and enable session scope."""

    global _engine, _session_factory, _db_path, _temp_db_path, _delete_temp_file

    async with _init_lock:
        if _engine is not None:
            return

        path, delete_temp, temp_path = _resolve_db_path(raw_path)
        url = _sqlite_url(path)

        engine_kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if path == ":memory:":
            engine_kwargs["poolclass"] = StaticPool
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            engine_kwargs["poolclass"] = NullPool

        engine: AsyncEngine | None = None
        session_factory: async_sessionmaker[AsyncSession] | None = None
        try:
            engine = create_async_engine(url, **engine_kwargs)
            session_factory = async_sessionmaker(
                engine,
                expire_on_commit=False,
                class_=AsyncSession,
            )

            _ensure_pragmas(engine)
            await _create_schema(engine)
        except Exception:
            if engine is not None:
                await engine.dispose()
            if delete_temp and temp_path:
                temp_path.unlink(missing_ok=True)
            raise

        assert engine is not None
        assert session_factory is not None
        _engine = engine
        _session_factory = session_factory
        _db_path = path
        _temp_db_path = temp_path
        _delete_temp_file = delete_temp

        from mcp_servers.uspto.cache.search_cache import reset_search_cache_metrics

        reset_search_cache_metrics()

        _register_atexit()


async def cleanup_db() -> None:
    """Dispose the SQLite engine and remove temporary storage if present."""

    global _engine, _session_factory, _db_path, _temp_db_path, _delete_temp_file

    async with _init_lock:
        if _engine is None:
            return

        await _engine.dispose()
        _engine = None
        _session_factory = None
        _db_path = None

        from mcp_servers.uspto.cache.search_cache import reset_search_cache_metrics

        reset_search_cache_metrics()

        if _delete_temp_file and _temp_db_path:
            _temp_db_path.unlink(missing_ok=True)
        _temp_db_path = None
        _delete_temp_file = False


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session for the current MCP session."""

    if _session_factory is None:
        raise RuntimeError("Database has not been initialized. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def current_db_path() -> str | None:
    """Return the normalized path used by the current SQLite engine."""

    return _db_path


def temp_db_path() -> str | None:
    """Return the temporary file path when a temp-mode database was created."""

    return _temp_db_path.as_posix() if _temp_db_path else None


async def check_db_connection() -> bool:
    """Check if the database connection is healthy.

    Returns:
        True if database is initialized and connection is working, False otherwise.
    """
    if _engine is None or _session_factory is None:
        return False

    try:
        async with _session_factory() as session:
            # Execute a simple query to verify connectivity
            await session.execute(text("SELECT 1"))
            return True
    except Exception:
        return False
