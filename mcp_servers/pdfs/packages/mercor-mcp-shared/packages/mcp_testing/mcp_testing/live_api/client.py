"""HTTP client for making live API requests."""

from typing import Any

import httpx


class HTTPClient:
    """Generic HTTP client for calling live APIs."""

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        """Initialize HTTP client.

        Args:
            base_url: Base URL for the API
            auth_token: Optional authentication token (Bearer)
            headers: Additional headers to include in requests
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        # Create a copy to avoid mutating caller's headers
        self.headers = dict(headers) if headers else {}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"
        if "Content-Type" not in self.headers:
            self.headers["Content-Type"] = "application/json"
        if "Accept" not in self.headers:
            self.headers["Accept"] = "application/json"

    async def request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | str | int | float | bool]:
        """Make an HTTP request to the live API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            data: Request body data
            params: Query parameters
            headers: Override headers for this request

        Returns:
            Tuple of (status_code, response_data) where response_data can be any valid JSON type
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        request_headers = {**self.headers, **(headers or {})}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method=method.upper(),
                url=url,
                json=data,
                params=params,
                headers=request_headers,
            )

            try:
                response_data = response.json()
            except Exception:
                response_data = {"text": response.text}

            return response.status_code, response_data
