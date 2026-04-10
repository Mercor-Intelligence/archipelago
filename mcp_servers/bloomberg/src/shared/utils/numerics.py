import math
from typing import Any

import pandas as pd


def is_valid_value(v: Any) -> bool:
    """Return True if v is a scalar and not NaN/None."""
    try:
        return bool(pd.notna(v)) and not isinstance(v, (pd.Series, pd.DataFrame))
    except Exception:
        return False


def to_float_safe(value: Any, default: float | None = 0.0) -> float | None:
    """
    Convert value to float safely.
    - Returns `default` for None or non-convertible values.
    - Preserves NaN as None instead of replacing with default.
    """
    if value is None:
        return default
    try:
        result = float(value)
        if math.isnan(result):
            return None
        return result
    except (ValueError, TypeError):
        return default


def to_int_safe(value: Any, default: int = 0) -> int | None:
    """
    Convert value to int safely.
    - Returns `None` for NaN values.
    - Returns `default` for None / non-convertible values.
    """
    if value is None:
        return default
    if pd.isna(value):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default
