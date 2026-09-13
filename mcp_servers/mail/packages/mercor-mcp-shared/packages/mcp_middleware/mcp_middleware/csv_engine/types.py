"""Shared type helpers for CSV parsing and formatting."""

import re
from datetime import datetime
from typing import Any


def normalize_header(h: str) -> str:
    """Normalize a CSV header to canonical snake_case.

    Converts CamelCase, spaces, hyphens to snake_case so that
    'DocNumber', 'Doc Number', 'doc_number' all match the same column.
    """
    if not h:
        return ""
    s = h.strip()
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = s.lower()
    s = s.replace(" ", "_").replace("-", "_")
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def parse_date(date_str: str) -> datetime:
    """Parse date string in various formats into datetime."""
    if not date_str or not date_str.strip():
        raise ValueError("Empty date string")
    date_formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%m-%d-%Y",
        "%d-%m-%Y",
        "%m/%d/%y",
        "%d/%m/%y",
        "%y/%m/%d",
    ]
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse date: {date_str}")


def parse_decimal(value_str: str) -> float:
    """Parse string to float, removing currency symbols and commas."""
    if not value_str or value_str.strip() == "":
        return 0.0
    cleaned = value_str.replace("$", "").replace(",", "").strip()
    return float(cleaned)


def parse_bool(value_str: str) -> bool:
    """Parse string to boolean."""
    if not value_str:
        return False
    return value_str.lower() in ("true", "yes", "1", "t", "y")


def format_date(dt: datetime | str | None) -> str:
    """Format datetime to ISO date string for CSV.

    Accepts datetime objects, strings (returned as-is), or None.
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d")


def format_decimal(value: Any) -> str:
    """Format decimal/float to string for CSV."""
    if value is None:
        return ""
    return str(value)


def format_bool(value: bool | None) -> str:
    """Format boolean to 'true'/'false' string for CSV."""
    if value is None:
        return ""
    return "true" if value else "false"
