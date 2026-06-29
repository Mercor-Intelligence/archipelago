"""Populate subsystems with data from S3-compatible storage or direct upload."""

from .main import handle_populate
from .streaming import handle_populate_stream

__all__ = ["handle_populate", "handle_populate_stream"]
