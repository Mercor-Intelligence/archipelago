"""ISIN validation utilities for Terrapin bond tools."""

import re

ISIN_PATTERN = re.compile(r"^[A-Z0-9]{12}$")


def validate_isin(isin: str) -> str | None:
    """Validate and sanitize a single ISIN. Returns cleaned ISIN or None if invalid."""
    cleaned = isin.strip().upper()
    if ISIN_PATTERN.match(cleaned):
        return cleaned
    return None


def validate_isins(isins: list[str]) -> tuple[list[str], list[str]]:
    """Validate a list of ISINs. Returns (valid, invalid) tuple."""
    valid: list[str] = []
    invalid: list[str] = []
    for isin in isins:
        result = validate_isin(isin)
        if result:
            valid.append(result)
        else:
            invalid.append(isin)
    return valid, invalid
