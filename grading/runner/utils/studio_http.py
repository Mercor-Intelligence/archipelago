"""Shared retry helpers for RL Studio internal API calls from Archipelago grading."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

_RETRYABLE_GATEWAY_STATUS_CODES = frozenset({502, 503, 504})
_RETRYABLE_TRANSIENT_4XX = frozenset({408, 429})


def is_retryable_studio_http_failure(exc: BaseException) -> bool:
    """Return True for transient transport, gateway, rate-limit, and 5xx failures."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return (
            status_code in _RETRYABLE_GATEWAY_STATUS_CODES
            or status_code in _RETRYABLE_TRANSIENT_4XX
            or status_code >= 500
        )
    return False


# Back-compat alias used by modal_helpers tests.
_is_retryable_studio_error = is_retryable_studio_http_failure


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(is_retryable_studio_http_failure),
    reraise=True,
)
async def studio_get_json(
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    *,
    timeout: httpx.Timeout | float = 120.0,
) -> Any:
    """GET JSON from RL Studio, retrying transient failures."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(is_retryable_studio_http_failure),
    reraise=True,
)
async def studio_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: httpx.Timeout | float = 300.0,
) -> httpx.Response:
    """POST JSON to RL Studio, retrying transient failures."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response
