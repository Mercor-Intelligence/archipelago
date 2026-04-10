"""Loader classes for inserting data into databases."""

from .base import BaseLoader
from .duckdb import DuckDBLoader

__all__ = ["BaseLoader", "DuckDBLoader"]
