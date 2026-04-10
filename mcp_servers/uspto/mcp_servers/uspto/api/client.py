"""USPTO Open Data Portal API client with offline/online support."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import email.utils as eut
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from mcp_servers.uspto.utils.transform import (
    normalize_key,
    normalize_mime_type,
    transform_application_details,
    transform_documents,
    transform_foreign_priority,
    transform_search_results,
    transform_status_codes,
)

OFFLINE_ERROR_RESPONSE: dict[str, Any] = {
    "error": {
        "code": "OFFLINE_MODE_ACTIVE",
        "message": "USPTO API is running in offline mode. No live data available.",
        "details": {
            "suggestion": "Restart server with --online flag to enable live USPTO API calls",
            "offlineMode": True,
        },
    }
}

# Path to shared static status codes file (fallback when API unavailable)
STATUS_CODES_FALLBACK_FILE = (
    Path(__file__).parent.parent.parent.parent / "data" / "uspto" / "status_codes.json"
)


class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, rate_per_minute: int, period_seconds: float = 60.0) -> None:
        safe_rate = max(rate_per_minute, 1)
        self.capacity = float(safe_rate)
        self.tokens = float(self.capacity)
        self.refill_rate = float(safe_rate) / float(period_seconds)
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()
        self._period = period_seconds

    async def acquire(self) -> None:
        """Block until a token is available."""

        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.updated_at
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                if self.tokens >= 1:
                    self.tokens -= 1
                    self.updated_at = now
                    return
                missing = 1 - self.tokens
                wait_seconds = missing / self.refill_rate if self.refill_rate else self._period
                # Update the timestamp so elapsed does not double-count after waiting
                self.updated_at = now
            await asyncio.sleep(wait_seconds)


def _normalize_application_number(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or value


def _transform_item(item: Any) -> Any:
    if isinstance(item, list):
        return [_transform_item(i) for i in item]

    if not isinstance(item, dict):
        if isinstance(item, str):
            return normalize_mime_type(item)
        return item

    transformed: dict[str, Any] = {}
    for key, value in item.items():
        if key == "applicationMetaData" and isinstance(value, dict):
            for nested_key, nested_value in value.items():
                normalized_nested_key = str(normalize_key(nested_key))
                transformed[normalized_nested_key] = _transform_item(nested_value)
            continue

        normalized_key = normalize_key(key)
        transformed[normalized_key] = _transform_item(value)
    return transformed


def transform_uspto_response(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Transform USPTO API response to normalized format."""

    transformed: dict[str, Any] = {}
    for key, value in raw_response.items():
        normalized_key = normalize_key(key)
        if key.endswith("Bag") and isinstance(value, list):
            transformed[normalized_key] = [_transform_item(item) for item in value]
        else:
            transformed[normalized_key] = _transform_item(value)

    transformed["raw_uspto_response"] = raw_response
    return transformed


class USPTOAPIClient:
    """USPTO Open Data Portal API client with offline/online mode support."""

    def __init__(
        self,
        api_key: str | None = None,
        # USPTO Open Data Portal API
        base_url: str = "https://api.uspto.gov/api/v1/patent",
        offline_mode: bool = True,
        rate_limiter: RateLimiter | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        http_limits: httpx.Limits | None = None,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.offline_mode = offline_mode
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._max_retries = max_retries

        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "USPTO-MCP-Server/0.1.0",
        }
        if api_key:
            headers["X-API-KEY"] = api_key

        limits = http_limits or httpx.Limits(max_connections=100, max_keepalive_connections=20)
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            headers=headers,
            http2=self._supports_http2(),
            limits=limits,
        )
        if http_client:
            self._client.headers.update(headers)

        self._rate_limiters = self._build_rate_limiters(rate_limiter)

    def _build_rate_limiters(self, provided: RateLimiter | None) -> dict[str, RateLimiter]:
        if provided:
            return {
                "search": provided,
                "application": provided,
                "status": provided,
                "documents": provided,
                "foreign": provided,
            }

        return {
            "search": RateLimiter(50),
            "application": RateLimiter(100),
            "status": RateLimiter(10),
            "documents": RateLimiter(50),
            "foreign": RateLimiter(50),
        }

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""

        await self._client.aclose()

    @staticmethod
    def _supports_http2() -> bool:
        return importlib.util.find_spec("h2") is not None

    @staticmethod
    def _offline_error() -> dict[str, Any]:
        return copy.deepcopy(OFFLINE_ERROR_RESPONSE)

    def _log_request(self, endpoint: str, params: dict[str, Any] | None) -> None:
        logger.bind(endpoint=endpoint).info(
            "Making USPTO API request",
            extra={
                "endpoint": endpoint,
                "api_key": "[REDACTED]",
                "params": params or {},
            },
        )

    async def _execute_with_retries(
        self,
        limiter_key: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[httpx.Response | None, dict[str, Any] | None]:
        if self.offline_mode:
            return None, self._offline_error()

        rate_limiter = self._rate_limiters.get(limiter_key)
        if rate_limiter:
            await rate_limiter.acquire()

        self._log_request(path, params)
        backoff = self._retry_base_delay

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(path, params=params)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    return None, self._error_response("NETWORK_ERROR", str(exc))
                await asyncio.sleep(backoff)
                backoff = min(self._retry_max_delay, backoff * 2)
                continue

            status = response.status_code
            if status == 429:
                if attempt >= self._max_retries:
                    return None, self._error_response(
                        "UPSTREAM_RATE_LIMITED",
                        "USPTO rate limit exceeded",
                        {"statusCode": status},
                    )
                retry_after_header = response.headers.get("Retry-After")
                retry_after_seconds: float = 0
                if retry_after_header:
                    try:
                        retry_after_seconds = float(retry_after_header)
                    except ValueError:
                        try:
                            retry_dt = eut.parsedate_to_datetime(retry_after_header)
                            if retry_dt:
                                if retry_dt.tzinfo is None:
                                    retry_dt = retry_dt.replace(tzinfo=dt.UTC)
                                retry_after_seconds = max(
                                    0,
                                    (retry_dt - dt.datetime.now(tz=dt.UTC)).total_seconds(),
                                )
                        except Exception:
                            retry_after_seconds = 0
                wait_time = max(min(retry_after_seconds, self._retry_max_delay), backoff)
                await asyncio.sleep(wait_time)
                backoff = min(self._retry_max_delay, backoff * 2)
                continue

            if 500 <= status < 600:
                if attempt >= self._max_retries:
                    return None, self._error_response(
                        "UPSTREAM_ERROR",
                        "USPTO upstream error",
                        {"statusCode": status},
                    )
                await asyncio.sleep(backoff)
                backoff = min(self._retry_max_delay, backoff * 2)
                continue

            if 400 <= status < 500 and status != 429:
                if status == 404:
                    return None, self._coverage_error(params)
                try:
                    upstream_message = response.json()
                except Exception:
                    upstream_message = {}
                return None, self._error_response(
                    "UPSTREAM_CLIENT_ERROR",
                    "USPTO rejected the request",
                    {"statusCode": status, "upstreamError": upstream_message},
                )

            return response, None

        return None, self._error_response("UNKNOWN_ERROR", "Unexpected failure")

    def _coverage_error(self, params: dict[str, Any] | None) -> dict[str, Any]:
        application_number = None
        if params and isinstance(params.get("applicationNumber"), str):
            application_number = params["applicationNumber"]
        message = "Requested resource is outside USPTO dataset coverage"
        details: dict[str, Any] = {"upstreamError": {"statusCode": 404, "message": "Not found"}}
        if application_number:
            details["applicationNumber"] = application_number
        return {
            "error": {
                "code": "DATASET_COVERAGE_UNAVAILABLE",
                "message": message,
                "details": details,
            }
        }

    def _error_response(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": code, "message": message}}
        if details is not None:
            payload["error"]["details"] = details
        return payload

    async def search_applications(
        self,
        query: str,
        filters: dict | None = None,
        start: int = 0,
        rows: int = 25,
        sort: str | None = None,
    ) -> dict:
        """Search published applications and issued patents."""

        if self.offline_mode:
            return self._offline_error()

        params = {
            "q": query,
            "start": start,
            "rows": rows,
        }
        if filters:
            params["filters"] = json.dumps(filters, sort_keys=True)
        if sort:
            params["sort"] = sort

        response, error = await self._execute_with_retries(
            "search",
            "applications/search",
            params=params,
        )
        if error:
            return error
        assert response is not None

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            return self._error_response(
                "UPSTREAM_INVALID_JSON",
                "USPTO returned malformed JSON",
                {"reason": str(exc)},
            )
        transformed = transform_search_results(data)
        if "totalFound" in data:
            transformed["total"] = data.get("totalFound")
        return transformed

    async def get_application(self, application_number: str) -> dict:
        """Retrieve application details by number."""

        if self.offline_mode:
            return self._offline_error()

        normalized = _normalize_application_number(application_number)
        params = {"applicationNumber": normalized}
        response, error = await self._execute_with_retries(
            "application",
            f"applications/{normalized}",
            params=params,
        )
        if error:
            return error
        assert response is not None

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            return self._error_response(
                "UPSTREAM_INVALID_JSON",
                "USPTO returned malformed JSON",
                {"reason": str(exc)},
            )
        return transform_application_details(data)

    async def _load_static_status_codes(self) -> dict[str, Any] | None:
        """Load status codes from static fallback file.

        Returns:
            Transformed status codes dict, or None if file unavailable.
        """
        if not STATUS_CODES_FALLBACK_FILE.exists():
            return None

        def _load() -> dict[str, Any]:
            with open(STATUS_CODES_FALLBACK_FILE) as f:
                return json.load(f)

        try:
            # Run file I/O in thread pool to avoid blocking event loop
            raw_data = await asyncio.to_thread(_load)
            logger.info("Loaded status codes from static fallback file")
            return transform_status_codes(raw_data)
        except Exception as exc:
            logger.warning(f"Failed to load static status codes: {exc}")
            return None

    async def get_status_codes(self) -> dict:
        """Retrieve authoritative status code reference table.

        Falls back to static file if API is unavailable.
        """
        if self.offline_mode:
            return self._offline_error()

        # Note: Tool handles session-scoped caching, not client
        # USPTO API paginates status codes (default 25, max 100 per page)
        # We need to fetch all pages to get the complete list (typically 241 codes)
        all_status_codes: list[dict] = []
        offset = 0
        limit = 100  # Max allowed per request
        total_count = None
        version = None  # Capture version from first page if present

        while True:
            # Note: endpoint is "status-codes" (with hyphen), not "statusCodes"
            params = {"limit": limit, "offset": offset}
            response, error = await self._execute_with_retries(
                "status", "status-codes", params=params
            )
            if error:
                if not all_status_codes:
                    # First page failed, try static fallback
                    logger.warning("USPTO status codes API failed, trying static fallback")
                    fallback = await self._load_static_status_codes()
                    if fallback:
                        return fallback
                    return error
                # We got some data, return what we have
                break
            assert response is not None

            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                if not all_status_codes:
                    # Try static fallback
                    fallback = await self._load_static_status_codes()
                    if fallback:
                        return fallback
                    return self._error_response(
                        "UPSTREAM_INVALID_JSON",
                        "USPTO returned malformed JSON",
                        {"reason": str(exc)},
                    )
                break

            # Get total count and version from first response
            if total_count is None:
                # Use infinity if count missing/null to ensure we paginate fully
                count = data.get("count")
                total_count = float("inf") if count is None else count
                version = data.get("version")  # May be None if USPTO doesn't provide

            # Extract status codes from this page
            # Use `or []` to handle both missing keys AND null values
            page_codes = data.get("statusCodeBag") or []
            all_status_codes.extend(page_codes)

            # Check if we've fetched all codes
            if len(all_status_codes) >= total_count or len(page_codes) < limit:
                break

            offset += limit

        result: dict[str, Any] = {
            "count": len(all_status_codes),
            "statusCodeBag": all_status_codes,
        }
        if version is not None:
            result["version"] = version
        return transform_status_codes(result)

    async def get_documents(
        self,
        application_number: str,
        start: int = 0,
        rows: int = 100,
    ) -> dict:
        """Retrieve document inventory for application."""

        if self.offline_mode:
            return self._offline_error()

        params = {"start": start, "rows": rows}
        normalized = _normalize_application_number(application_number)
        response, error = await self._execute_with_retries(
            "documents",
            f"applications/{normalized}/documents",
            params={**params, "applicationNumber": normalized},
        )
        if error:
            return error
        assert response is not None

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            return self._error_response(
                "UPSTREAM_INVALID_JSON",
                "USPTO returned malformed JSON",
                {"reason": str(exc)},
            )
        return transform_documents(data)

    async def get_foreign_priority(self, application_number: str) -> dict:
        """Retrieve foreign priority claims for application."""

        if self.offline_mode:
            return self._offline_error()

        normalized = _normalize_application_number(application_number)
        paths = [
            f"applications/{normalized}/foreignPriority",
            f"applications/{normalized}/foreign-priority",
            f"applications/{normalized}/foreign-priorities",
        ]

        param_sets: list[dict[str, Any] | None] = [
            {"applicationNumber": normalized},
            None,
        ]
        if application_number != normalized:
            param_sets.extend(
                [
                    {"applicationNumberText": application_number},
                    {"applicationNumber": application_number},
                ]
            )

        def _should_retry(error_payload: dict[str, Any] | None) -> bool:
            if not error_payload:
                return False
            error_info = error_payload.get("error", {})
            if error_info.get("code") != "UPSTREAM_CLIENT_ERROR":
                return False
            details = error_info.get("details", {})
            upstream_error = details.get("upstreamError", {})
            message = upstream_error.get("message") if isinstance(upstream_error, dict) else None
            if isinstance(message, str) and "Missing Authentication Token" in message:
                return True
            return details.get("statusCode") == 400

        last_error: dict[str, Any] | None = None
        for path in paths:
            for params in param_sets:
                response, error = await self._execute_with_retries(
                    "foreign",
                    path,
                    params=params,
                )
                if error:
                    last_error = error
                    if _should_retry(error):
                        continue
                    return error

                assert response is not None
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    return self._error_response(
                        "UPSTREAM_INVALID_JSON",
                        "USPTO returned malformed JSON",
                        {"reason": str(exc)},
                    )
                return transform_foreign_priority(data)

        if last_error is not None:
            return last_error
        return self._error_response("UNKNOWN_ERROR", "Unexpected failure")

    async def generate_patent_pdf(self, application_number: str) -> dict[str, Any]:
        """Generate a patent PDF from offline database content."""
        if self.offline_mode:
            return self._offline_error()

        return self._error_response(
            "UNSUPPORTED_OPERATION",
            "Patent PDF generation is only available in offline mode.",
            {"applicationNumber": application_number},
        )

    async def ping(self) -> bool:
        """Lightweight health check for USPTO API availability."""
        if self.offline_mode:
            return False

        try:
            # Make a lightweight HEAD request to the base URL
            # This avoids consuming API quota while checking availability
            await self._client.head("/", timeout=5.0)
            # Accept any response (even 404) as long as the server responds
            return True
        except Exception:
            return False


__all__ = [
    "USPTOAPIClient",
    "RateLimiter",
    "transform_uspto_response",
]
