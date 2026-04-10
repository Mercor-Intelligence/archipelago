import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from openbb_app.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class EquityProvider(BaseProvider):
    """Handles equity data requests via OpenBB."""

    async def fetch_quote(self, ticker: str, provider: str | None = None) -> dict[str, Any]:
        """Fetch real-time quote data."""
        if not provider:
            available = self.client.get_working_providers()
            provider = self.get_preferred_provider(available)

        logger.info(f"Fetching quote for {ticker} via {provider}")

        try:
            result = self._obb.equity.price.quote(symbol=ticker, provider=provider)  # type: ignore

            if hasattr(result, "to_dict"):
                data: dict[str, Any] = result.to_dict()  # type: ignore
                return data

            if result.results:
                data: dict[str, Any] = result.results[0].model_dump()
                return data

            return {}

        except Exception as e:
            logger.error(f"Error fetching quote for {ticker}: {e}")
            raise

    async def screen_equity(
        self, filters: dict[str, Any], provider: str | None = None
    ) -> list[dict[str, Any]]:
        """Execute equity screen."""
        if not provider:
            available = self.client.get_working_providers()
            provider = self.get_preferred_provider(available)

        logger.info(f"Screening equities via {provider}: {filters}")

        try:
            # Actual implementation depends on provider capabilities
            result = self._obb.equity.screener(provider=provider, **filters)  # type: ignore

            results = self.extract_results(result)
            return results

        except Exception as e:
            logger.error(f"Error screening equities: {e}")
            raise

    def extract_results(self, result):
        # If it has to_dict method, use that
        if hasattr(result, "results") and result.results:
            output = []
            for r in result.results:
                if is_dataclass(r):
                    output.append(asdict(r))  # type: ignore
                else:
                    # fallback for Pydantic models
                    output.append(getattr(r, "model_dump", lambda r=r: r)())
            return output

        # fallback: check if to_dict exists
        if hasattr(result, "to_dict"):
            data: dict[str, Any] = result.to_dict()  # type: ignore
            # If "results" key exists, use it, else empty list
            return data.get("results", [])

        return []
