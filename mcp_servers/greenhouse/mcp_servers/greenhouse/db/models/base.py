"""Base model and common utilities for Greenhouse database models."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


def utc_now() -> datetime:
    """Get current UTC time."""
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """Get current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class TimestampMixin:
    """Mixin that adds created_at and updated_at timestamp columns.

    Uses ISO 8601 string format to match Greenhouse API responses.
    """

    @declared_attr
    def created_at(self) -> Mapped[str]:  # noqa: N805
        return mapped_column(String, default=utc_now_iso, nullable=False)

    @declared_attr
    def updated_at(self) -> Mapped[str]:  # noqa: N805
        return mapped_column(String, default=utc_now_iso, onupdate=utc_now_iso, nullable=False)


class TimestampMixinDatetime:
    """Mixin using native datetime objects instead of ISO strings.

    Use this when you need datetime operations in Python.
    """

    @declared_attr
    def created_at(self) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(DateTime, default=utc_now, nullable=False)

    @declared_attr
    def updated_at(self) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)
