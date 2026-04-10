"""API client for Terrapin Finance API with offline mode support.

When offline mode is active, requests are routed to local fixtures.
When online, validates API key and makes requests to the remote API.
"""

import httpx
from loguru import logger
from utils.config import OFFLINE_MODE, TERRAPIN_API_BASE_URL, TERRAPIN_API_KEY


class InvalidAPIKeyError(Exception):
    """Raised when API key is present but invalid."""

    pass


class OfflineModeError(Exception):
    """Raised when trying to make API calls in offline mode without fixtures."""

    pass


def validate_api_key() -> bool:
    """Validate the API key by making a test request.

    Returns:
        True if API key is valid

    Raises:
        InvalidAPIKeyError: If API key is present but invalid
    """
    if OFFLINE_MODE:
        return False

    if not TERRAPIN_API_KEY:
        return False

    try:
        with httpx.Client(
            base_url=TERRAPIN_API_BASE_URL,
            headers={
                "Authorization": f"Bearer {TERRAPIN_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        ) as client:
            # Make a minimal request to validate the key
            # Using bond_search with limit=1 as a lightweight validation
            response = client.post("/bond_search", json={"limit": 1})

            if response.status_code == 401:
                raise InvalidAPIKeyError(
                    "TERRAPIN_API_KEY is invalid. Please check your API key. "
                    "If you want to use offline mode, use --offline=true"
                )
            elif response.status_code == 403:
                raise InvalidAPIKeyError(
                    "TERRAPIN_API_KEY does not have sufficient permissions. "
                    "Please check your API key permissions."
                )

            # Other errors are not necessarily key issues
            return response.status_code == 200

    except httpx.ConnectError:
        logger.warning("Could not connect to Terrapin API for key validation")
        return False
    except httpx.TimeoutException:
        logger.warning("Timeout while validating API key")
        return False


def get_api_client() -> httpx.Client:
    """Get an authenticated HTTP client for Terrapin API.

    Returns:
        httpx.Client configured for Terrapin API

    Raises:
        OfflineModeError: If called in offline mode
        InvalidAPIKeyError: If API key is invalid
    """
    if OFFLINE_MODE:
        raise OfflineModeError(
            "Cannot create API client in offline mode. Use fixture functions instead."
        )

    if not TERRAPIN_API_KEY:
        raise OfflineModeError("TERRAPIN_API_KEY not set. Running in offline mode.")

    return httpx.Client(
        base_url=TERRAPIN_API_BASE_URL,
        headers={
            "Authorization": f"Bearer {TERRAPIN_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def is_offline_mode() -> bool:
    """Check if the server is running in offline mode."""
    return OFFLINE_MODE
