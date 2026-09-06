"""SQLAlchemy declarative base for FMP models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all FMP database models.

    All model classes should inherit from this base to be registered
    with SQLAlchemy's metadata for table creation and management.
    """

    pass
