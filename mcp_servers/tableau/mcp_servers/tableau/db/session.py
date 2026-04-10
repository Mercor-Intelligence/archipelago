"""Database session management.

Provides async SQLAlchemy session for database operations.
"""

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from db.models import Datasource, Project, Site, User, View, Workbook, WorkbookDatasource
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

# Fixed UUIDs for default data (matches Alembic migrations)
DEFAULT_SITE_ID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
DEFAULT_USER_ID = "b1c2d3e4-f5a6-4b5c-8d9e-0f1a2b3c4d5e"
DEFAULT_PROJECT_ID = "c2d3e4f5-a6b7-4c5d-9e0f-1a2b3c4d5e6f"

# Global variables (lazily initialized)
_engine = None
_session_maker = None


def get_database_url():
    """Get database URL from environment or default.

    Priority:
    1. TABLEAU_DATABASE_URL if explicitly set
    2. Derived from STATE_LOCATION if set (for RL Studio snapshots)
    3. Default ./data.db fallback
    """
    # If TABLEAU_DATABASE_URL is explicitly set, use it
    if db_url := os.getenv("TABLEAU_DATABASE_URL"):
        return db_url

    # Derive from STATE_LOCATION if set (RL Studio convention)
    # This ensures the database is captured in trajectory snapshots
    state_location = os.getenv("STATE_LOCATION")
    if state_location:
        return f"sqlite+aiosqlite:///{state_location}/data.db"

    # Default to file-based SQLite in the server directory for persistence
    default_path = Path(__file__).resolve().parent.parent / "data.db"
    return f"sqlite+aiosqlite:///{default_path}"


def get_engine():
    """Get or create database engine (lazy initialization)."""
    global _engine
    if _engine is None:
        db_url = get_database_url()
        engine_kwargs: dict = {"echo": False}

        # In-memory SQLite needs StaticPool so all connections share the same DB
        if ":memory:" in db_url:
            engine_kwargs["poolclass"] = StaticPool
            engine_kwargs["connect_args"] = {"check_same_thread": False}

        _engine = create_async_engine(db_url, **engine_kwargs)

        # Enable foreign key constraints for SQLite
        if "sqlite" in db_url:

            @event.listens_for(_engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_session_maker():
    """Get or create session maker (lazy initialization)."""
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_maker


async def init_db():
    """Initialize database and seed default data.

    Note: Tables should be created via Alembic migrations (run `mise run build`).
    This function only ensures the directory exists and seeds default data.
    """
    # Clear CSV file storage from previous session
    from tools.visualization_tools import clear_csv_storage

    clear_csv_storage()

    # Ensure directory exists for SQLite database
    db_url = get_database_url()
    logger.info(f"[init_db] TABLEAU_DATABASE_URL={db_url}")

    if "sqlite" in db_url:
        # Extract path from sqlite+aiosqlite:///path/to/data.db
        db_path = db_url.split("///")[-1]
        if db_path and db_path != ":memory:":
            parent_dir = Path(db_path).parent
            if parent_dir and str(parent_dir) != ".":
                parent_dir.mkdir(parents=True, exist_ok=True)

        # Debug: check if db file exists
        db_file = Path(db_path)
        logger.info(
            f"[init_db] db_path={db_path}, exists={db_file.exists()}, size={db_file.stat().st_size if db_file.exists() else 'N/A'}"
        )

    # Drop leftover CSV tables and their ORM records from previous sessions
    async with get_engine().begin() as conn:
        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = [row[0] for row in result.fetchall()]
        csv_tables = [t for t in tables if t.startswith("csv_")]
        for table in csv_tables:
            if not re.match(r"^[a-zA-Z0-9_]+$", table):
                logger.warning(f"Skipping CSV table with unsafe name: {table!r}")
                continue
            await conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
        if csv_tables:
            logger.info(f"Dropped {len(csv_tables)} CSV tables from previous session")

    # Clean up Datasource/View/WorkbookDatasource/Workbook records for old CSV uploads
    async with get_session_maker()() as session:
        csv_datasources = (
            (await session.execute(select(Datasource).where(Datasource.connection_type == "csv")))
            .scalars()
            .all()
        )
        workbook_ids_to_delete: set[str] = set()
        for ds in csv_datasources:
            views = (
                (await session.execute(select(View).where(View.datasource_id == ds.id)))
                .scalars()
                .all()
            )
            for v in views:
                if v.workbook_id:
                    workbook_ids_to_delete.add(v.workbook_id)
                await session.delete(v)
            wb_ds = (
                (
                    await session.execute(
                        select(WorkbookDatasource).where(WorkbookDatasource.datasource_id == ds.id)
                    )
                )
                .scalars()
                .all()
            )
            for wd in wb_ds:
                await session.delete(wd)
            # Flush child deletions before deleting datasource to satisfy FK constraints
            # (no ORM relationships defined, so SQLAlchemy can't determine delete order)
            await session.flush()
            await session.delete(ds)
        # Flush datasource deletions so the SET NULL FK fires for any remaining
        # views that reference these datasources (e.g. views created outside CSV flow)
        if csv_datasources:
            await session.flush()
        # Delete Workbook records only if they have no remaining views or
        # workbook-datasource links (to avoid FK violations).
        for wb_id in workbook_ids_to_delete:
            remaining_views = (
                (await session.execute(select(View).where(View.workbook_id == wb_id)))
                .scalars()
                .all()
            )
            if remaining_views:
                continue
            remaining_wb_ds = (
                (
                    await session.execute(
                        select(WorkbookDatasource).where(WorkbookDatasource.workbook_id == wb_id)
                    )
                )
                .scalars()
                .all()
            )
            if remaining_wb_ds:
                continue
            wb = (
                await session.execute(select(Workbook).where(Workbook.id == wb_id))
            ).scalar_one_or_none()
            if wb:
                await session.delete(wb)
        await session.commit()
        if csv_datasources:
            logger.info(f"Cleaned up {len(csv_datasources)} CSV datasource records")

    # Seed default site, user, and project if they don't exist
    # This replicates the Alembic migrations for essential FK targets
    async with get_session_maker()() as session:
        now = datetime.now(timezone.utc)

        result = await session.execute(select(Site).where(Site.id == DEFAULT_SITE_ID))
        if not result.scalar_one_or_none():
            session.add(
                Site(
                    id=DEFAULT_SITE_ID,
                    name="Default",
                    content_url="",
                    created_at=now,
                    updated_at=now,
                )
            )

        result = await session.execute(select(User).where(User.id == DEFAULT_USER_ID))
        if not result.scalar_one_or_none():
            session.add(
                User(
                    id=DEFAULT_USER_ID,
                    site_id=DEFAULT_SITE_ID,
                    name="Demo User",
                    email="demo@example.com",
                    site_role="Creator",
                    created_at=now,
                    updated_at=now,
                )
            )

        result = await session.execute(select(Project).where(Project.id == DEFAULT_PROJECT_ID))
        if not result.scalar_one_or_none():
            session.add(
                Project(
                    id=DEFAULT_PROJECT_ID,
                    site_id=DEFAULT_SITE_ID,
                    name="Weather Analytics",
                    description="Demo project containing weather analysis dashboards",
                    parent_project_id=None,
                    owner_id=DEFAULT_USER_ID,
                    created_at=now,
                    updated_at=now,
                )
            )

        await session.commit()


@asynccontextmanager
async def get_session():
    """Get database session.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(MyModel))
    """
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def reset_engine():
    """Reset engine and session maker (used for testing)."""
    global _engine, _session_maker
    _engine = None
    _session_maker = None
