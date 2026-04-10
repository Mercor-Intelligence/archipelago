"""Database session management for Xero MCP."""

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

# =============================================================================
# Database Path Configuration
# =============================================================================
#
# Path = {APPS_DATA_ROOT}/xero/data.db
#
# Same structure everywhere - only root differs:
#   - RL Studio (default): /.apps_data/xero/data.db
#   - Local dev: ./.apps_data/xero/data.db
#
# Local setup: export APPS_DATA_ROOT=./.apps_data
#
# =============================================================================

APP_NAME = "xero"
APPS_DATA_ROOT = os.environ.get("APPS_DATA_ROOT", "/.apps_data")


def get_data_path() -> Path:
    """Get database path: {APPS_DATA_ROOT}/xero/data.db"""
    return Path(APPS_DATA_ROOT) / APP_NAME / "data.db"


def get_database_url() -> str:
    """Get database URL. XERO_DATABASE_URL env var takes precedence (for tests)."""
    env_url = os.environ.get("XERO_DATABASE_URL")
    if env_url:
        return env_url
    data_path = get_data_path()
    return f"sqlite+aiosqlite:///{data_path}"


XERO_DATABASE_URL = get_database_url()

# Create async engine
engine = create_async_engine(
    XERO_DATABASE_URL,
    echo=False,  # Set to True for SQL debugging
    future=True,
)

# Create session factory
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Base class for all models
Base = declarative_base()


async def init_db():
    """Initialize database tables."""
    from loguru import logger

    db_path = get_data_path()
    db_dir = db_path.parent

    logger.info(f"XERO_DATABASE_URL={XERO_DATABASE_URL}")

    if not db_dir.exists():
        logger.warning(
            f"Database directory {db_dir} does not exist — creating it. "
            f"This should have been created by the platform (fix_permissions in start.sh)."
        )
        db_dir.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db():
    """Drop all database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
