"""
Field Calculators for Approximated Bloomberg Fields

Provides calculation strategies for fields that require computation
from base OHLCV data (e.g., VWAP, TURNOVER).
"""

from typing import Protocol

import pandas as pd


class FieldCalculator(Protocol):
    """Protocol for field calculation strategies."""

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Calculate field values from OHLCV data.

        Args:
            df: DataFrame with OHLCV data (columns: high, low, close, volume)

        Returns:
            Series with calculated values
        """
        ...


class TurnoverCalculator:
    """
    Calculate trading turnover (value traded).

    Formula: TURNOVER = volume × close price
    """

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Calculate turnover as volume × close price.

        Args:
            df: DataFrame with 'volume' and 'close' columns

        Returns:
            Series with turnover values
        """
        if "volume" not in df.columns or "close" not in df.columns:
            return pd.Series([None] * len(df), index=df.index)

        return df["volume"] * df["close"]


class VWAPCalculator:
    """
    Calculate Volume-Weighted Average Price (VWAP).

    Formula: VWAP = Σ(typical_price × volume) / Σ(volume)
    where typical_price = (high + low + close) / 3

    Note: This is a cumulative calculation, so VWAP increases over time
    as more data points are included.
    """

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """
        Calculate VWAP as cumulative volume-weighted typical price.

        Args:
            df: DataFrame with 'high', 'low', 'close', 'volume' columns

        Returns:
            Series with VWAP values
        """
        required_cols = ["high", "low", "close", "volume"]
        if not all(col in df.columns for col in required_cols):
            return pd.Series([None] * len(df), index=df.index)

        # Calculate typical price (average of high, low, close)
        typical_price = (df["high"] + df["low"] + df["close"]) / 3

        # VWAP = cumulative sum of (typical_price × volume) / cumulative sum of volume
        cumulative_tpv = (typical_price * df["volume"]).cumsum()
        cumulative_volume = df["volume"].cumsum()

        # Avoid division by zero
        return cumulative_tpv / cumulative_volume.replace(0, pd.NA)


# Registry of available field calculators
FIELD_CALCULATORS: dict[str, FieldCalculator] = {
    "TURNOVER": TurnoverCalculator(),
    "VWAP": VWAPCalculator(),
}


def calculate_field(field_name: str, df: pd.DataFrame) -> pd.Series | None:
    """
    Calculate a field value using the appropriate calculator.

    Args:
        field_name: Bloomberg field mnemonic (e.g., "VWAP", "TURNOVER")
        df: DataFrame with OHLCV data

    Returns:
        Series with calculated values, or None if no calculator exists
    """
    calculator = FIELD_CALCULATORS.get(field_name)
    if calculator is None:
        return None

    return calculator.calculate(df)
