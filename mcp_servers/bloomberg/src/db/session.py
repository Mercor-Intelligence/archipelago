"""Database session management and base classes."""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# DB path from env var, or default for local dev
# In Docker: runs from repo root, so relative path works
DEFAULT_DB_PATH = Path(os.environ.get("DUCKDB_PATH", "data/offline.duckdb"))
INTRADAY_INTERVALS = ["1min", "5min", "15min", "30min", "1hour", "4hour"]


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""

    pass


def get_engine(db_path: Path | str | None = None, read_only: bool = False) -> Engine:
    """Create SQLAlchemy engine for DuckDB.

    Args:
        db_path: Path to DuckDB file. Defaults to data/offline.duckdb
        read_only: Whether to open read-only

    Returns:
        SQLAlchemy Engine
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    connect_args = {"read_only": read_only}
    return create_engine(f"duckdb:///{path}", connect_args=connect_args)


def create_session(db_path: Path | str | None = None, read_only: bool = True) -> Session:
    """Create a database session.

    Args:
        db_path: Path to DuckDB file. Defaults to data/offline.duckdb
        read_only: Whether to open read-only (default True)

    Returns:
        SQLAlchemy Session
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")

    engine = get_engine(path, read_only)
    session_factory = sessionmaker(bind=engine)
    return session_factory()


class DatabaseSession:
    """Context manager for database sessions.

    Usage:
        with DatabaseSession() as session:
            prices = HistoricalPrice.find_by_symbol(session, "AAPL")
    """

    def __init__(self, db_path: Path | str | None = None, read_only: bool = True):
        self.db_path = db_path
        self.read_only = read_only
        self._session: Session | None = None
        self._engine: Engine | None = None

    def __enter__(self) -> Session:
        path = Path(self.db_path) if self.db_path else DEFAULT_DB_PATH
        if not path.exists():
            raise FileNotFoundError(f"Database not found: {path}")
        self._engine = get_engine(path, self.read_only)
        session_factory = sessionmaker(bind=self._engine)
        self._session = session_factory()
        return self._session

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._session:
            self._session.close()
        if self._engine:
            self._engine.dispose()


def create_all_tables(db_path: Path | str | None = None) -> None:
    """Create all tables from ORM models.

    Args:
        db_path: Path to DuckDB file
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine(path, read_only=False)
    Base.metadata.create_all(engine)
