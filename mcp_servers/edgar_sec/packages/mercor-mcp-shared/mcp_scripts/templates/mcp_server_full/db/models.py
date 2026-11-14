"""SQLAlchemy database models for __SNAKE_NAME__.

These are your database/ORM models (separate from Pydantic API models).
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now():
    """Get current UTC time."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class __PASCAL_NAME__(Base):
    """Example database model for __SNAKE_NAME__.

    TODO: Update fields to match your database schema.
    """

    __tablename__ = "__SNAKE_NAME___items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
