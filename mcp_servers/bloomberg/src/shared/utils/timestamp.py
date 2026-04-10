import logging
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def extract_iso_timestamp(row: pd.Series, df: pd.DataFrame) -> str | None:
    """
    Extracts a timestamp from a DataFrame row and returns it as an ISO 8601 string.

    It checks for common timestamp column names and supports:
    - Python datetime/date objects
    - pandas.Timestamp
    - numpy.datetime64
    - string fallback

    Args:
        row (pd.Series): The current DataFrame row.
        df (pd.DataFrame): The full DataFrame (used to check available columns).

    Returns:
        str | None: ISO 8601 formatted timestamp, or None if no timestamp found.
    """
    timestamp = None
    for col in ["date", "datetime", "timestamp", "index"]:
        if col in df.columns:
            timestamp = row[col]
            break

    if timestamp is None:
        logger.warning("No timestamp column found in DataFrame")
        return None

    # Convert to ISO 8601 string
    if isinstance(timestamp, (datetime, pd.Timestamp)):
        return timestamp.isoformat()
    elif np.isscalar(timestamp) and np.issubdtype(type(timestamp), np.datetime64):
        return pd.to_datetime(timestamp).isoformat()  # type: ignore
    else:
        return str(timestamp)
