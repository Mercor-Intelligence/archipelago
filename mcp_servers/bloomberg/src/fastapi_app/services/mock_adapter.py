"""
Mock Adapter for Testing and Development

Provides synthetic historical data for testing without requiring OpenBB/network access.
Useful for CI/CD, unit tests, and development.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from fastapi_app.models.enums import PeriodicitySelection
from shared.models import create_security_error

logger = logging.getLogger(__name__)


class MockAdapter:
    """
    Mock data adapter that generates synthetic historical data.

    Useful for:
    - CI/CD testing without network calls
    - Unit tests
    - Development when OpenBB is unavailable
    - Load testing
    """

    def __init__(self, seed: int = 42):
        """
        Initialize mock adapter.

        Args:
            seed: Random seed for reproducible data generation
        """
        self._seed = seed
        random.seed(seed)

        # Predefined "valid" securities for testing
        self._valid_securities = {
            "AAPL US Equity": {"base_price": 150.0, "volatility": 0.02},
            "MSFT US Equity": {"base_price": 300.0, "volatility": 0.015},
            "GOOGL US Equity": {"base_price": 120.0, "volatility": 0.025},
            "TSLA US Equity": {"base_price": 200.0, "volatility": 0.04},
            "AMZN US Equity": {"base_price": 130.0, "volatility": 0.02},
        }

    def _is_valid_security(self, security: str) -> bool:
        """Check if security is in the valid set."""
        return security in self._valid_securities

    def _generate_price_series(
        self, base_price: float, volatility: float, num_periods: int
    ) -> list[dict[str, float]]:
        """
        Generate synthetic OHLCV data using random walk.

        Args:
            base_price: Starting price
            volatility: Daily volatility (std dev)
            num_periods: Number of periods to generate

        Returns:
            List of dicts with OHLCV data
        """
        data = []
        current_price = base_price

        for _ in range(num_periods):
            # Random walk
            change = random.gauss(0, volatility)
            current_price *= 1 + change

            # Generate OHLC around current price
            daily_range = current_price * random.uniform(0.01, 0.03)
            high = current_price + random.uniform(0, daily_range)
            low = current_price - random.uniform(0, daily_range)
            open_price = random.uniform(low, high)
            close_price = current_price

            # Volume (random between 10M and 100M)
            volume = random.randint(10_000_000, 100_000_000)

            data.append(
                {
                    "open": round(open_price, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(close_price, 2),
                    "volume": volume,
                }
            )

        return data

    def _generate_date_range(
        self, start_date: datetime, end_date: datetime, periodicity: PeriodicitySelection
    ) -> list[datetime]:
        """
        Generate date range based on periodicity.

        Args:
            start_date: Start date
            end_date: End date
            periodicity: Periodicity selection

        Returns:
            List of dates
        """
        dates = []
        current = start_date

        if periodicity == PeriodicitySelection.DAILY:
            delta = timedelta(days=1)
        elif periodicity == PeriodicitySelection.WEEKLY:
            delta = timedelta(weeks=1)
        elif periodicity == PeriodicitySelection.MONTHLY:
            delta = timedelta(days=30)  # Approximate
        elif periodicity == PeriodicitySelection.QUARTERLY:
            delta = timedelta(days=90)  # Approximate
        elif periodicity == PeriodicitySelection.YEARLY:
            delta = timedelta(days=365)  # Approximate
        else:
            delta = timedelta(days=1)

        while current <= end_date:
            # Skip weekends for daily data
            if periodicity == PeriodicitySelection.DAILY:
                if current.weekday() < 5:  # Monday=0, Friday=4
                    dates.append(current)
            else:
                dates.append(current)

            current += delta

        return dates

    async def fetch_historical_data(
        self,
        security: str,
        fields: list[str],
        start_date: datetime,
        end_date: datetime,
        periodicity: PeriodicitySelection = PeriodicitySelection.DAILY,
        **kwargs,
    ) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
        """
        Generate mock historical data for a security.

        Args:
            security: Bloomberg security identifier
            fields: List of Bloomberg field mnemonics
            start_date: Start date
            end_date: End date
            periodicity: Data periodicity
            **kwargs: Additional parameters (ignored)

        Returns:
            Tuple of (DataFrame with data, error dict if failed)
        """
        # Simulate network latency
        await asyncio.sleep(random.uniform(0.05, 0.2))

        # Check if security is valid
        if not self._is_valid_security(security):
            error = create_security_error(
                security=security, reason=f"Unknown/Invalid security: {security}"
            )
            return None, error.model_dump()

        # Get security parameters
        sec_params = self._valid_securities[security]

        # Generate date range
        dates = self._generate_date_range(start_date, end_date, periodicity)

        if not dates:
            return pd.DataFrame(), None

        # Generate price data
        ohlcv_data = self._generate_price_series(
            base_price=sec_params["base_price"],
            volatility=sec_params["volatility"],
            num_periods=len(dates),
        )

        # Create DataFrame
        df = pd.DataFrame(ohlcv_data)
        df["date"] = dates

        # Parse fields to Bloomberg format using OpenBBAdapter
        from fastapi_app.services.openbb_adapter import OpenBBAdapter

        result = OpenBBAdapter.parse_fields(df, fields, "HistoricalDataRequest")

        return result, None

    async def fetch_multiple_securities(
        self,
        securities: list[str],
        fields: list[str],
        start_date: datetime,
        end_date: datetime,
        periodicity: PeriodicitySelection = PeriodicitySelection.DAILY,
        **kwargs,
    ) -> list[tuple[str, pd.DataFrame | None, dict[str, Any] | None]]:
        """
        Fetch mock historical data for multiple securities.

        Args:
            securities: List of Bloomberg security identifiers
            fields: List of Bloomberg field mnemonics
            start_date: Start date
            end_date: End date
            periodicity: Data periodicity
            **kwargs: Additional parameters

        Returns:
            List of tuples (security, DataFrame, error) for each security
        """
        tasks = [
            self.fetch_historical_data(
                security, fields, start_date, end_date, periodicity, **kwargs
            )
            for security in securities
        ]

        results = await asyncio.gather(*tasks)

        return [
            (security, df, error) for security, (df, error) in zip(securities, results, strict=True)
        ]


# Singleton instance
mock_adapter = MockAdapter()
