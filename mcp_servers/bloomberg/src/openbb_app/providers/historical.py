import asyncio
import logging
from typing import Any

from openbb_app.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class HistoricalProvider(BaseProvider):
    async def fetch_historical(
        self,
        securities: list[str],
        fields: list[str],
        start_date: str,
        end_date: str,
        interval: str = "1d",
        adjustment: str = "all",
        provider: str | None = None,
    ) -> list[tuple[str, Any, Any]]:
        """
        Fetch historical data for multiple securities from a specific provider.

        Returns a list of tuples: (security, dataframe or None, error or None)
        """
        results: list[tuple[str, Any, Any]] = []

        for security in securities:
            try:
                df = await asyncio.to_thread(
                    self._fetch_single_security,
                    security,
                    fields,
                    start_date,
                    end_date,
                    interval,
                    adjustment,
                    provider,
                )
                results.append((security, df, None))
            except Exception as e:
                logger.error(f"Error fetching historical for {security}: {e}")
                results.append((security, None, {"message": str(e)}))

        return results

    def _fetch_single_security(
        self,
        security: str,
        fields: list[str],
        start_date: str,
        end_date: str,
        interval: str,
        adjustment: str,
        provider: str | None = None,
    ):
        """
        Synchronous call to OpenBB client for a single security, with optional provider.
        """
        chosen_provider = provider or self.get_preferred_provider(
            self._obb.stocks.historical.get_providers()  # type: ignore
        )
        logger.info(
            f"Fetching historical for {security} from {chosen_provider}: {start_date} → {end_date}, interval={interval}, adjustment={adjustment}"
        )

        df = self._obb.stocks.historical.load(  # type: ignore
            symbol=security,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
            adj=adjustment,
            provider=chosen_provider,
        )

        # Filter fields if needed
        if fields:
            missing_fields = [f for f in fields if f not in df.columns]
            if missing_fields:
                logger.warning(f"Fields missing in result for {security}: {missing_fields}")
            df = df[[f for f in fields if f in df.columns]]

        return df
