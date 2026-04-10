"""Base provider with OpenBB client access."""

import logging

from openbb_app.openbb_client import OpenBBClient

logger = logging.getLogger(__name__)


class BaseProvider:
    """Base class for OpenBB data providers."""

    def __init__(self, client: OpenBBClient):
        self.client = client
        self._obb = client.client  # The actual obb instance

    # TODO: Revisit this
    def get_preferred_provider(self, available: list[str]) -> str:
        """Select best available provider from list."""
        priority = ["yfinance", "finviz", "intrinio", "polygon"]
        for p in priority:
            if p in available:
                return p
        return available[0] if available else "yfinance"
