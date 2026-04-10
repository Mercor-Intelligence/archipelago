"""Looker API authentication.

Implements OAuth2 authentication flow for Looker API:
1. Exchange client_id + client_secret for access_token
2. Cache token and handle refresh

References:
- https://cloud.google.com/looker/docs/api-auth
- https://cloud.google.com/looker/docs/reference/looker-api/latest/methods/ApiAuth/login
"""

import asyncio
from datetime import datetime, timedelta

from http_client import get_http_client
from loguru import logger


class LookerAuthService:
    """Manages Looker API authentication tokens.

    Handles OAuth2 token exchange and caching for Looker API.
    Tokens are cached and refreshed automatically before expiry.
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        verify_ssl: bool = True,
        timeout: int = 120,
    ):
        """Initialize auth service.

        Args:
            base_url: Looker instance URL (e.g., https://company.looker.com:19999)
            client_id: Looker API client ID
            client_secret: Looker API client secret
            verify_ssl: Whether to verify SSL certificates
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.verify_ssl = verify_ssl
        self.timeout = timeout

        # Token cache
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        """Get a valid access token, refreshing if necessary.

        Returns:
            Valid access token for API requests

        Raises:
            httpx.HTTPError: If authentication fails
        """
        async with self._lock:
            # Check if we have a valid cached token
            if self._access_token and self._token_expires_at:
                # Refresh 5 minutes before expiry to be safe
                if datetime.now() + timedelta(minutes=5) < self._token_expires_at:
                    logger.debug("Using cached Looker access token")
                    return self._access_token

            # Need to get a new token
            logger.info("Fetching new Looker access token")
            await self._refresh_token()
            return self._access_token

    async def _refresh_token(self) -> None:
        """Call Looker /login endpoint to get a new access token.

        Updates self._access_token and self._token_expires_at.

        Raises:
            httpx.HTTPError: If login fails
        """
        login_url = f"{self.base_url}/api/4.0/login"

        client = get_http_client()
        # Looker login accepts credentials in request body (more secure than query params)
        response = await client.post(
            login_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )

        response.raise_for_status()
        token_data = response.json()

        # Extract token and expiry
        self._access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)  # Default 1 hour
        self._token_expires_at = datetime.now() + timedelta(seconds=expires_in)

        logger.info(f"Looker access token acquired, expires in {expires_in}s")

    def clear_cache(self) -> None:
        """Clear cached token (for testing or logout)."""
        self._access_token = None
        self._token_expires_at = None
        logger.debug("Cleared Looker token cache")
