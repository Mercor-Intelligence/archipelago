"""Mock HTTP client for testing without live API calls."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class MockResponse:
    """Represents a mock API response."""

    status_code: int
    data: dict[str, Any] | list[Any]
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> dict[str, Any] | list[Any]:
        """Return response data (mimics httpx.Response)."""
        return self.data


class MockAPIClient:
    """
    Mock HTTP client for testing MCP servers without hitting live APIs.

    Example:
        ```python
        from mcp_testing import MockAPIClient, LiveAPIComparator

        # Setup mock responses
        mock_client = MockAPIClient(base_url="https://api.example.com")
        mock_client.add_response(
            "GET", "/users",
            status=200,
            data={"users": [{"id": 1, "name": "John"}]}
        )

        # Use in comparator
        comparator = LiveAPIComparator(
            mock_tool=my_mcp_tool,
            http_client=mock_client,
        )

        # Test without hitting real API
        result = await comparator.compare_endpoint("/users", "GET", {})
        ```
    """

    def __init__(self, base_url: str = "https://api.mock.com", **kwargs):
        """
        Initialize mock API client.

        Args:
            base_url: Base URL for the mock API
            **kwargs: Additional arguments (ignored, for compatibility)
        """
        self.base_url = base_url.rstrip("/")
        self._responses: dict[tuple[str, str], MockResponse] = {}
        self._default_response = MockResponse(
            status_code=404, data={"error": "Mock response not configured"}
        )
        self._call_history: list[dict[str, Any]] = []

    def _normalize_endpoint(self, endpoint: str) -> str:
        """Normalize endpoint to a consistent format for lookup.

        Uses urllib.parse (stdlib) for URL parsing, with custom logic for
        our specific normalization needs (strip query strings, base_url, etc).

        Handles full URLs, base_url prefixes, and relative paths.
        Strips query strings for consistent matching.

        Args:
            endpoint: Endpoint to normalize

        Returns:
            Normalized endpoint path
        """
        # Strip query string first
        if "?" in endpoint:
            endpoint = endpoint.split("?")[0]

        # Extract path from full URL if provided
        if endpoint.startswith(("http://", "https://")):
            parsed = urlparse(endpoint)
            endpoint = parsed.path
        elif endpoint.startswith(self.base_url):
            # Starts with base_url string - strip it
            endpoint = endpoint[len(self.base_url) :]

        # Ensure endpoint starts with slash for consistent comparison
        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        # If base_url contains a path component, strip it from endpoint
        base_parsed = urlparse(self.base_url)
        if base_parsed.path:
            base_path = base_parsed.path.rstrip("/")
            # Check if endpoint starts with base path
            if endpoint.startswith(base_path + "/"):
                endpoint = endpoint[len(base_path) :]
            elif endpoint == base_path:
                endpoint = "/"

        # Strip leading slash
        return endpoint.lstrip("/")

    def add_response(
        self,
        method: str,
        endpoint: str,
        status: int = 200,
        data: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Add a mock response for a specific endpoint.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path (query strings are stripped for matching)
            status: HTTP status code
            data: Response data
            headers: Response headers
        """
        endpoint = self._normalize_endpoint(endpoint)
        key = (method.upper(), endpoint)
        self._responses[key] = MockResponse(
            status_code=status,
            data=data if data is not None else {},
            headers=headers or {},
        )

    def add_responses(self, responses: dict[tuple[str, str], dict[str, Any]]) -> None:
        """
        Add multiple mock responses at once.

        Args:
            responses: Dict mapping (method, endpoint) to response config

        Example:
            ```python
            mock_client.add_responses({
                ("GET", "/users"): {
                    "status": 200,
                    "data": {"users": [...]},
                },
                ("POST", "/users"): {
                    "status": 201,
                    "data": {"id": 1},
                },
            })
            ```
        """
        for (method, endpoint), response_config in responses.items():
            self.add_response(
                method=method,
                endpoint=endpoint,
                status=response_config.get("status", 200),
                data=response_config.get("data"),
                headers=response_config.get("headers"),
            )

    def set_default_response(
        self,
        status: int = 404,
        data: dict[str, Any] | None = None,
    ) -> None:
        """
        Set default response for unmocked endpoints.

        Args:
            status: Default HTTP status code
            data: Default response data
        """
        self._default_response = MockResponse(
            status_code=status,
            data=data if data is not None else {"error": "Not found"},
        )

    async def request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        """
        Make a mock HTTP request.

        Args:
            method: HTTP method
            endpoint: API endpoint path
            data: Request body data
            params: Query parameters
            headers: Request headers

        Returns:
            Tuple of (status_code, response_data)
        """
        # Normalize endpoint using shared method
        endpoint = self._normalize_endpoint(endpoint)

        # Record call
        self._call_history.append(
            {
                "method": method.upper(),
                "endpoint": endpoint,
                "data": data,
                "params": params,
                "headers": headers,
            }
        )

        # Get response
        key = (method.upper(), endpoint)
        response = self._responses.get(key, self._default_response)

        return response.status_code, response.data

    def get_call_history(self) -> list[dict[str, Any]]:
        """
        Get history of all calls made to the mock client.

        Returns:
            List of call details (method, endpoint, data, params, headers)
        """
        return self._call_history.copy()

    def reset(self) -> None:
        """Clear all responses and call history."""
        self._responses.clear()
        self._call_history.clear()

    def assert_called(
        self,
        method: str,
        endpoint: str,
        times: int | None = None,
    ) -> None:
        """
        Assert that an endpoint was called.

        Args:
            method: HTTP method
            endpoint: API endpoint path
            times: Expected number of calls (None = at least once)

        Raises:
            AssertionError: If assertion fails
        """
        endpoint = self._normalize_endpoint(endpoint)
        calls = [
            c
            for c in self._call_history
            if c["method"] == method.upper() and c["endpoint"] == endpoint
        ]

        if times is None:
            assert len(calls) > 0, f"{method} {endpoint} was not called"
        else:
            assert len(calls) == times, (
                f"{method} {endpoint} was called {len(calls)} times, expected {times}"
            )

    def assert_called_with(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        """
        Assert that an endpoint was called with specific parameters.

        Args:
            method: HTTP method
            endpoint: API endpoint path
            data: Expected request body
            params: Expected query parameters

        Raises:
            AssertionError: If assertion fails
        """
        endpoint = self._normalize_endpoint(endpoint)
        matching_calls = [
            c
            for c in self._call_history
            if c["method"] == method.upper()
            and c["endpoint"] == endpoint
            and (data is None or c["data"] == data)
            and (params is None or c["params"] == params)
        ]

        assert len(matching_calls) > 0, (
            f"{method} {endpoint} was not called with expected parameters"
        )
