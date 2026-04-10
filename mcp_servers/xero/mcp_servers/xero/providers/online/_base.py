"""Base functionality for online provider."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, overload

import httpx
from loguru import logger
from mcp_cache.cache_middleware import CachedHTTPClient

from mcp_servers.xero.auth.oauth_manager import OAuthManager
from mcp_servers.xero.config import Config
from mcp_servers.xero.providers.exceptions import (
    XeroApiAuthenticationError,
    XeroApiClientError,
    XeroApiNetworkError,
    XeroApiNotFoundError,
    XeroApiPermissionError,
    XeroApiRateLimitError,
    XeroApiServerError,
    XeroApiValidationError,
)

SleepCallable = Callable[[float], Awaitable[None]]


@dataclass
class _TenantBudget:
    """Track request budgets per tenant."""

    per_minute: int
    per_day: int
    clock: Callable[[], float]
    minute_start: float = 0.0
    minute_count: int = 0
    day_start: float = 0.0
    day_count: int = 0

    def __post_init__(self) -> None:
        now = self.clock()
        self.minute_start = now
        self.day_start = now

    def consume(self) -> None:
        """Consume a request budget slot or raise when exceeded."""
        now = self.clock()
        if self.per_minute > 0 and now - self.minute_start >= 60:
            self.minute_start = now
            self.minute_count = 0

        if self.per_day > 0 and now - self.day_start >= 86400:
            self.day_start = now
            self.day_count = 0

        if self.per_minute > 0 and self.minute_count >= self.per_minute:
            raise XeroApiRateLimitError(
                f"Client-side per-minute budget ({self.per_minute}) exhausted; wait before retrying.",
                status_code=429,
                problem="minute-budget",
            )

        if self.per_day > 0 and self.day_count >= self.per_day:
            raise XeroApiRateLimitError(
                f"Client-side daily budget ({self.per_day}) exhausted; wait for reset.",
                status_code=429,
                problem="daily-budget",
            )

        self.minute_count += 1
        self.day_count += 1


class OnlineProviderBase:
    """
    Base class for online provider with shared authentication and request logic.

    Provides OAuth token management, deterministic retry/backoff handling, and
    thread-safe client-side rate limit enforcement using asyncio.Lock.

    Thread-safety:
        Rate limiting is thread-safe using asyncio.Lock per tenant, ensuring
        atomic budget consumption when multiple coroutines make concurrent requests.
    """

    _RETRYABLE_STATUS = {429, 502, 503, 504}

    def __init__(
        self,
        config: Config,
        oauth_manager: OAuthManager,
        *,
        http_client: CachedHTTPClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        sleep_fn: SleepCallable | None = None,
        rng: random.Random | None = None,
    ):
        """
        Initialize online provider base.

        Args:
            config: Application configuration
            oauth_manager: OAuth manager for authentication
            http_client: Optional pre-configured cached http client
            transport: Optional custom transport (used when constructing client)
            clock: Optional monotonic clock override (tests)
            sleep_fn: Optional awaitable sleep function (tests)
            rng: Optional RNG for deterministic jitter (tests)
        """
        self.config = config
        self.oauth_manager = oauth_manager
        self._clock = clock or time.monotonic
        self._sleep: SleepCallable = sleep_fn or asyncio.sleep
        if rng:
            self._rng = rng
        else:
            seed = config.backoff_jitter_seed
            self._rng = random.Random(seed if seed is not None else time.time_ns())

        self._tenant_budgets: dict[str, _TenantBudget] = {}
        self._tenant_locks: dict[str, asyncio.Lock] = {}
        self._init_lock: asyncio.Lock = asyncio.Lock()
        self._last_request_headers: dict[str, str] | None = None

        self._http_timeout = 30.0

        if http_client is not None:
            self.client = http_client
        elif transport is not None:
            base_client = httpx.AsyncClient(timeout=self._http_timeout, transport=transport)
            self.client = CachedHTTPClient(
                base_client=base_client,
                enable_caching=False,
                respect_cache_control=False,
            )
        else:
            base_client = httpx.AsyncClient(timeout=self._http_timeout)
            self.client = CachedHTTPClient(
                base_client=base_client,
                enable_caching=True,
                respect_cache_control=True,
            )

        self._using_custom_transport = transport is not None

        logger.info("Initializing online provider with HTTP caching and retries")

    @overload
    async def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        *,
        return_pagination: Literal[False] = False,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    @overload
    async def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        *,
        return_pagination: Literal[True],
        **kwargs: Any,
    ) -> tuple[dict[str, Any], bool]: ...

    async def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        *,
        return_pagination: bool = False,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | tuple[dict[str, Any], bool]:
        """
        Make authenticated request to Xero API.

        Args:
            endpoint: API endpoint (path after base URL)
            method: HTTP method
            **kwargs: Additional request parameters

        Returns:
            Response data as dict
        """
        tenant_id = self.config.xero_tenant_id
        if not tenant_id:
            raise ValueError("No tenant selected. Please configure XERO_TENANT_ID.")

        access_token = await self.oauth_manager.get_valid_access_token()
        if not access_token:
            raise XeroApiAuthenticationError("Failed to obtain valid access token.")

        method_upper = method.upper()
        effective_base_url = base_url or self.config.xero_api_base_url
        if getattr(self, "_using_custom_transport", False):
            base = httpx.URL(effective_base_url)
            url = base.copy_with(path=endpoint)
        else:
            url = httpx.URL(f"{effective_base_url.rstrip('/')}/{endpoint.lstrip('/')}")
        request_kwargs = dict(kwargs)
        first_request_kwargs = dict(request_kwargs)

        response = await self._request_with_retries(
            url=str(url),
            method=method_upper,
            tenant_id=tenant_id,
            access_token=access_token,
            base_kwargs=first_request_kwargs,
            endpoint=endpoint,
        )

        data = response.json()
        has_next_after_aggregation = False
        if method_upper == "GET" and isinstance(data, dict):
            data, has_next_after_aggregation = await self._collect_paginated_response(
                initial_data=data,
                initial_response=response,
                endpoint=endpoint,
                method=method_upper,
                tenant_id=tenant_id,
                access_token=access_token,
                original_kwargs=request_kwargs,
                base_url=effective_base_url,
            )

        # Always remove _pagination_info from response to avoid leaking internal metadata
        if isinstance(data, dict):
            data.pop("_pagination_info", None)

        if return_pagination:
            return data, has_next_after_aggregation

        return data

    async def _request_with_retries(
        self,
        url: str,
        method: str,
        tenant_id: str,
        access_token: str,
        base_kwargs: dict[str, Any],
        endpoint: str,
    ) -> httpx.Response:
        """Execute HTTP request with retry/backoff handling."""
        max_attempts = max(1, self.config.max_retries + 1)
        last_exception: Exception | None = None
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            await self._enforce_client_rate_limit(tenant_id)
            request_kwargs = dict(base_kwargs)
            extra_headers_mapping = request_kwargs.pop("headers", None)
            extra_headers = dict(extra_headers_mapping) if extra_headers_mapping else {}
            headers = self._build_headers(access_token, tenant_id, extra_headers)
            request_kwargs["headers"] = headers

            logger.debug(f"[Attempt {attempt}/{max_attempts}] {method} {url}")
            try:
                response = await self.client.request(method, url, **request_kwargs)
            except httpx.TimeoutException as exc:
                if isinstance(exc, httpx.ReadTimeout):
                    last_exception = XeroApiNetworkError(
                        "Network timeout while calling Xero API.",
                        details={"endpoint": endpoint},
                    )
                    if not self._can_retry(method, attempt, max_attempts):
                        raise last_exception from exc
                    await self._sleep(self._next_backoff_delay(attempt))
                    continue

                last_exception = exc
                if not self._can_retry(method, attempt, max_attempts):
                    raise exc
                await self._sleep(self._next_backoff_delay(attempt))
                continue
            except httpx.RequestError as exc:
                last_exception = XeroApiNetworkError(
                    "Network error while calling Xero API.",
                    details={"endpoint": endpoint},
                )
                if not self._can_retry(method, attempt, max_attempts):
                    raise last_exception from exc
                await self._sleep(self._next_backoff_delay(attempt))
                continue

            status = response.status_code
            if status < 400:
                return response

            if method != "GET" and (status == 429 or 500 <= status < 600):
                raise self._map_http_error(response)

            error = self._map_http_error(response)
            last_error = error

            if status == 429:
                if response.headers.get("X-Rate-Limit-Problem"):
                    raise error

                if self._can_retry(method, attempt, max_attempts):
                    delay = self._next_backoff_delay(attempt, response)
                    logger.warning(f"{status} from Xero for {endpoint}; retrying in {delay:.2f}s")
                    await self._sleep(delay)
                    continue

                # always raise XeroApiRateLimitError to preserve error contract
                # (retry_after and problem attributes are set in error)
                raise error

            if self._is_retryable(status) and self._can_retry(method, attempt, max_attempts):
                delay = self._next_backoff_delay(attempt, response)
                logger.warning(f"{status} from Xero for {endpoint}; retrying in {delay:.2f}s")
                await self._sleep(delay)
                continue

            raise error

        if last_error:
            raise last_error
        if last_exception:
            raise last_exception

        raise XeroApiServerError("Exhausted retries without receiving a valid response from Xero.")

    async def _collect_paginated_response(
        self,
        initial_data: dict[str, Any],
        initial_response: httpx.Response,
        endpoint: str,
        method: str,
        tenant_id: str,
        access_token: str,
        original_kwargs: dict[str, Any],
        base_url: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Follow X-Next-Page headers and aggregate list payloads.

        Returns:
            Tuple of (aggregated_data, has_next) where has_next indicates
            whether the initial response signaled additional pages via the
            ``x-next-page`` header.
        """
        next_page = initial_response.headers.get("x-next-page")
        saw_next_page_header = bool(next_page)
        if not next_page:
            return initial_data, False

        list_keys = [key for key, value in initial_data.items() if isinstance(value, list)]
        if not list_keys:
            return initial_data, saw_next_page_header

        aggregated = {key: list(initial_data.get(key, [])) for key in list_keys}
        base_params = dict(original_kwargs.get("params") or {})
        visited: set[str] = set()

        while next_page and next_page not in visited:
            visited.add(next_page)
            new_params = dict(base_params)
            new_params["page"] = next_page
            next_kwargs = dict(original_kwargs)
            next_kwargs["params"] = new_params

            effective_base_url = base_url or self.config.xero_api_base_url
            if getattr(self, "_using_custom_transport", False):
                base = httpx.URL(effective_base_url)
                page_url = base.copy_with(path=endpoint)
            else:
                page_url = httpx.URL(f"{effective_base_url.rstrip('/')}/{endpoint.lstrip('/')}")
            response = await self._request_with_retries(
                url=str(page_url),
                method=method,
                tenant_id=tenant_id,
                access_token=access_token,
                base_kwargs=next_kwargs,
                endpoint=endpoint,
            )
            page_data = response.json()
            for key in list_keys:
                aggregated.setdefault(key, [])
                aggregated[key].extend(page_data.get(key, []))

            next_page = response.headers.get("x-next-page")
            if next_page:
                saw_next_page_header = True

        for key, items in aggregated.items():
            initial_data[key] = items

        return initial_data, saw_next_page_header

    def _build_headers(
        self,
        access_token: str,
        tenant_id: str,
        extra_headers: dict[str, str],
    ) -> dict[str, str]:
        """Build request headers and capture for diagnostics."""
        headers = {
            "authorization": f"bearer {access_token}",
            "xero-tenant-id": tenant_id,
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": "mercor-xero-mcp/1.0",
        }
        headers.update(
            {(k.lower() if isinstance(k, str) else k): v for k, v in extra_headers.items()}
        )
        self._last_request_headers = dict(headers)
        return headers

    def _can_retry(self, method: str, attempt: int, max_attempts: int) -> bool:
        """Return True if request can be retried."""
        return method == "GET" and attempt < max_attempts

    def _is_retryable(self, status_code: int) -> bool:
        """Determine if HTTP status is eligible for retry."""
        return status_code in self._RETRYABLE_STATUS

    def _next_backoff_delay(
        self,
        attempt: int,
        response: httpx.Response | None = None,
    ) -> float:
        """Compute exponential backoff with jitter and Retry-After handling."""
        base = self.config.base_backoff * (2 ** (attempt - 1))
        jitter = self._rng.uniform(0, self.config.base_backoff)
        delay = min(self.config.max_backoff, base + jitter)

        retry_after = self._parse_retry_after(response)
        if retry_after is not None:
            delay = max(delay, retry_after)

        return max(0.0, delay)

    def _parse_retry_after(self, response: httpx.Response | None) -> float | None:
        """Parse Retry-After header from response."""
        if response is None:
            return None
        header = response.headers.get("Retry-After")
        if not header:
            return None
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                retry_dt = parsedate_to_datetime(header)
                retry_dt = retry_dt.astimezone(UTC)
                delta = (retry_dt - datetime.now(UTC)).total_seconds()
                return max(0.0, delta)
            except Exception:
                return None

    def _map_http_error(self, response: httpx.Response):
        """Map HTTP responses to typed exceptions."""
        status = response.status_code
        payload: dict[str, Any] | None = None
        text = response.text
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {"raw": str(payload)}
        except (ValueError, json.JSONDecodeError):
            payload = {"raw": text}

        message = (
            payload.get("message")
            or payload.get("Message")
            or payload.get("error_description")
            or response.reason_phrase
            or "Unexpected error"
        )

        if status == 400 or status == 422:
            return XeroApiValidationError(
                f"Xero rejected the request: {message}",
                status_code=status,
                details=payload,
            )
        if status == 401:
            guidance = (
                "Ensure the tenant is still connected and rerun the OAuth flow to refresh access."
            )
            return XeroApiAuthenticationError(
                f"Unauthorized from Xero: {message}. {guidance}",
                status_code=status,
                details=payload,
            )
        if status == 403:
            return XeroApiPermissionError(
                f"Insufficient permissions for tenant: {message}",
                status_code=status,
                details=payload,
            )
        if status == 404:
            return XeroApiNotFoundError(
                f"Resource not found: {message}",
                status_code=status,
                details=payload,
            )
        if status == 429:
            retry_after = self._parse_retry_after(response)
            problem = response.headers.get("X-Rate-Limit-Problem")
            return XeroApiRateLimitError(
                f"Rate limit exceeded: {problem or message}",
                status_code=status,
                details=payload,
                retry_after=retry_after,
                problem=problem,
            )
        if status >= 500:
            return XeroApiServerError(
                f"Xero service failure: {message}",
                status_code=status,
                details=payload,
            )

        return XeroApiClientError(
            f"Xero responded with HTTP {status}: {message}",
            status_code=status,
            details=payload,
        )

    async def _enforce_client_rate_limit(self, tenant_id: str) -> None:
        """
        Apply thread-safe client-side budget tracking per tenant.

        Uses asyncio.Lock to ensure atomicity of budget consumption operations,
        preventing race conditions when multiple coroutines make concurrent requests
        for the same tenant.

        Args:
            tenant_id: Tenant identifier for budget tracking

        Raises:
            XeroApiRateLimitError: If rate limit is exceeded
        """
        per_minute = self.config.rate_limit_per_minute
        per_day = self.config.rate_limit_per_day

        if per_minute <= 0 and per_day <= 0:
            return

        async with self._init_lock:
            if tenant_id not in self._tenant_budgets:
                self._tenant_budgets[tenant_id] = _TenantBudget(
                    per_minute=per_minute,
                    per_day=per_day,
                    clock=self._clock,
                )
                self._tenant_locks[tenant_id] = asyncio.Lock()

        async with self._tenant_locks[tenant_id]:
            self._tenant_budgets[tenant_id].consume()

    def get_last_request_headers(self) -> dict[str, str] | None:
        """Expose last request headers for testing instrumentation."""
        return self._last_request_headers.copy() if self._last_request_headers else None

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()
