"""Utility helpers for parsing USPTO date fields."""

from __future__ import annotations

from typing import Any


def coerce_iso_date(value: Any) -> str | None:
    """Return the first non-empty string from a date field that may be list-wrapped."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        for entry in value:
            if isinstance(entry, str) and entry:
                return entry
    return None
