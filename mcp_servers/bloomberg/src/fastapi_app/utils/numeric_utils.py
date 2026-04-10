import math
from typing import Any

import pandas as pd


def to_float(value: Any) -> float | None:
    """Safely convert a value to float, handling None, NaN, and pd.NA."""
    if value is None or value is pd.NA:
        return None
    try:
        result = float(value)
        if math.isnan(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    """Safely convert a value to int, handling None, NaN, and pd.NA."""
    if value is None or value is pd.NA:
        return None
    try:
        float_val = float(value)
        if math.isnan(float_val):
            return None
        return int(float_val)
    except (TypeError, ValueError):
        return None
