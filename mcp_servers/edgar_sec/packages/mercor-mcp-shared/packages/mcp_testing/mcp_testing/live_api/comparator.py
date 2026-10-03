"""Compare MCP mock server responses with live API responses."""

import time
from collections.abc import Callable
from typing import Any

from ..core.comparator import APIComparator
from ..core.models import ComparisonResult, Difference, DifferenceType, Severity
from .client import HTTPClient


class LiveAPIComparator(APIComparator):
    """Compare mock MCP server responses with live API responses.

    This class is the main entry point for live API testing. It calls both
    the mock server and live API, then compares their responses.

    Example:
        ```python
        comparator = LiveAPIComparator(
            mock_tool=my_mcp_tool,
            http_client=HTTPClient(
                base_url="https://api.example.com",
                auth_token="your_token"
            )
        )

        result = await comparator.compare_endpoint(
            endpoint="/v1/invoices",
            method="GET",
            request_data={"customer_id": "123"}
        )
        ```
    """

    def __init__(
        self,
        mock_tool: Callable[[Any], Any],
        http_client: HTTPClient,
        ignore_fields: list[str] | None = None,
        strict_mode: bool = False,
    ):
        """Initialize the live API comparator.

        Args:
            mock_tool: The MCP tool function to test
            http_client: HTTP client for calling the live API
            ignore_fields: Fields to ignore in comparison
            strict_mode: If True, treat all differences as errors
        """
        super().__init__(ignore_fields=ignore_fields, strict_mode=strict_mode)
        self.mock_tool = mock_tool
        self.http_client = http_client

    async def compare_endpoint(
        self,
        endpoint: str,
        method: str,
        request_data: dict[str, Any],
        mock_request_factory: Callable[[str, str, dict[str, Any]], Any] | None = None,
    ) -> ComparisonResult:
        """Compare mock and live API responses for a single endpoint.

        Args:
            endpoint: API endpoint path
            method: HTTP method
            request_data: Request payload or parameters
            mock_request_factory: Optional function to create mock request object

        Returns:
            ComparisonResult containing differences found
        """
        start_time = time.time()

        # Call mock API
        try:
            if mock_request_factory:
                mock_request = mock_request_factory(method, endpoint, request_data)
            else:
                mock_request = request_data

            mock_response = await self.mock_tool(mock_request)

            # Extract data from mock response (handle different response formats)
            if hasattr(mock_response, "data"):
                mock_data = mock_response.data
                mock_status = getattr(mock_response, "status_code", 200)
            elif hasattr(mock_response, "dict"):
                mock_dict = mock_response.dict()
                mock_data = mock_dict.get("data", mock_dict)
                mock_status = mock_dict.get("status_code", 200)
            elif isinstance(mock_response, dict):
                # Handle plain dict responses with status_code/data keys
                mock_data = mock_response.get("data", mock_response)
                mock_status = mock_response.get("status_code", 200)
            else:
                mock_data = mock_response
                mock_status = 200
        except Exception as e:
            # Mock tool crashed - treat as server error (500)
            mock_data = None
            mock_status = 500
            mock_error = str(e)
        else:
            mock_error = None

        # Call live API
        # Extract params and body from request_data
        params = request_data.get("params") if isinstance(request_data, dict) else None
        body = request_data.get("body") if isinstance(request_data, dict) else None
        # If request_data doesn't have params/body keys, use it as-is for backward compatibility
        if (
            isinstance(request_data, dict)
            and "params" not in request_data
            and "body" not in request_data
            and request_data
        ):
            body = request_data

        try:
            live_status, live_data = await self.http_client.request(
                method=method, endpoint=endpoint, params=params, data=body
            )
        except Exception as e:
            # Connection error, timeout, DNS failure, etc. - not a server 500
            # Use 0 to indicate no response received
            live_status = 0
            live_data = None
            live_error = str(e)
        else:
            live_error = None

        # Compare responses
        differences = []

        # Compare status codes
        if mock_status != live_status:
            differences.append(
                Difference(
                    type=DifferenceType.STATUS_CODE_MISMATCH,
                    path="status_code",
                    expected=live_status,
                    actual=mock_status,
                    severity=Severity.ERROR,
                    message=f"Status code mismatch: mock={mock_status}, live={live_status}",
                )
            )

        # Compare response data if both succeeded (check for None, not truthiness)
        if mock_data is not None and live_data is not None:
            field_diffs = self.compare_fields(live_data, mock_data)
            differences.extend(field_diffs)
        elif mock_data is None and live_data is not None:
            # Mock returned None but live has data
            differences.append(
                Difference(
                    type=DifferenceType.VALUE_MISMATCH,
                    path="data",
                    expected=live_data,
                    actual=None,
                    severity=Severity.ERROR,
                    message="Mock returned None but live API returned data",
                )
            )
        elif mock_data is not None and live_data is None:
            # Live returned None but mock has data
            differences.append(
                Difference(
                    type=DifferenceType.VALUE_MISMATCH,
                    path="data",
                    expected=None,
                    actual=mock_data,
                    severity=Severity.ERROR,
                    message="Live API returned None but mock returned data",
                )
            )

        # Handle errors
        if mock_error:
            differences.append(
                Difference(
                    type=DifferenceType.VALUE_MISMATCH,
                    path="mock_error",
                    expected=None,
                    actual=mock_error,
                    severity=Severity.ERROR,
                    message=f"Mock API error: {mock_error}",
                )
            )

        if live_error:
            differences.append(
                Difference(
                    type=DifferenceType.VALUE_MISMATCH,
                    path="live_error",
                    expected=live_error,
                    actual=None,
                    severity=Severity.ERROR,
                    message=f"Live API error: {live_error}",
                )
            )

        duration_ms = (time.time() - start_time) * 1000

        return ComparisonResult(
            endpoint=endpoint,
            method=method,
            request_data=request_data,
            mock_response=(
                {"status": mock_status, "data": mock_data} if mock_data is not None else None
            ),
            live_response=(
                {"status": live_status, "data": live_data} if live_data is not None else None
            ),
            differences=differences,
            duration_ms=duration_ms,
        )

    async def compare(
        self,
        endpoint: str,
        method: str,
        request_data: dict[str, Any],
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> ComparisonResult:
        """Compare expected and actual responses (implements abstract method).

        This is a simplified version used when you already have both responses.
        For most use cases, use compare_endpoint() instead.
        """
        differences = self.compare_fields(expected, actual)

        return ComparisonResult(
            endpoint=endpoint,
            method=method,
            request_data=request_data,
            mock_response=actual,
            live_response=expected,
            differences=differences,
        )
