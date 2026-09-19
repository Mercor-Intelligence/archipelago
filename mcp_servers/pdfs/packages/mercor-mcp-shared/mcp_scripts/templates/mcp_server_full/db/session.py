"""Database session management.

Provides async SQLAlchemy session for database operations.

Default: In-memory SQLite for reproducibility (clean state each session).
Override: Set DATABASE_URL env var for persistence.
"""

import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Base

# In-memory by default for reproducibility; override via DATABASE_URL env var
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

# echo=False is REQUIRED - echo=True prints SQL to stdout, breaking stdio JSON-RPC
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Initialize database (create tables)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session():
    """Get database session.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(MyModel))
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
