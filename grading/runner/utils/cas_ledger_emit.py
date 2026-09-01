"""Best-effort CAS ledger ingest for grading-runner LLM call sites.

Soft-imports ``mercor_cas_client`` (optional in island deliveries / slim test
envs). Never raises into the LLM call path.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Literal

from loguru import logger

try:
    from mercor_cas_client import CasClient, CasClientConfig
except ImportError:  # island deliveries skip the private package
    CasClient = None  # type: ignore[assignment,misc]
    CasClientConfig = None  # type: ignore[assignment,misc]

BackendLiteral = Literal["litellm", "direct"]

# Studio CAS schema v1 keys that grading spend headers already carry. Keep
# call_id / attempt out — they are correlation headers, not ledger tags (see
# rl-studio/infra/litellm/config.yaml's extra_spend_tag_headers allowlist,
# which does include call_id/attempt for proxy-side harvesting, but neither
# is declared in cas/registry/schemas/studio/v1.yaml).
_TAG_KEYS = frozenset(
    {
        "service",
        "campaign_id",
        "user_id",
        "account_id",
        "workload",
        "work_unit",
        "trajectory_id",
        "world_id",
        "batch_id",
        "grading_run_id",
        "purpose",
        "task_id",
    }
)
_PLACEHOLDER_CAMPAIGN = "no-campaign"


@lru_cache(maxsize=1)
def _client() -> Any | None:
    if CasClient is None or CasClientConfig is None:
        return None
    try:
        config = CasClientConfig.from_env()
        if not config.enabled:
            return None
        return CasClient(config)
    except Exception:
        logger.exception("CAS ledger client init failed; emit disabled")
        return None


def tags_from_spend_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Project spend-tag HTTP headers onto the CAS tag schema."""
    if not headers:
        return {"service": "grading", "campaign_id": _PLACEHOLDER_CAMPAIGN}
    tags = {k: str(v) for k, v in headers.items() if k in _TAG_KEYS and v}
    tags.setdefault("service", "grading")
    tags.setdefault("campaign_id", _PLACEHOLDER_CAMPAIGN)
    return tags


def record_cas_success(
    response: Any,
    *,
    model: str,
    tags: dict[str, str],
    backend: BackendLiteral = "litellm",
    latency_ms: int | None = None,
    raw_api_key: str | None = None,
) -> None:
    client: Any | None = None
    try:
        client = _client()
        if client is None or not client.enabled:
            return
        client.emit_from_litellm_response(
            response,
            model=model,
            backend=backend,
            status="success",
            tags=tags,
            latency_ms=latency_ms,
            raw_api_key=raw_api_key,
        )
    except Exception:
        logger.exception("CAS ledger success emit failed; preserving LLM semantics")
        try:
            if client is not None and getattr(client, "enabled", False):
                client.record_emission_failure()
        except Exception:
            logger.exception("Failed to count CAS emission failure")


def record_cas_failure(
    *,
    model: str,
    tags: dict[str, str],
    backend: BackendLiteral = "litellm",
    latency_ms: int | None = None,
    error_code: str | None = None,
    raw_api_key: str | None = None,
) -> None:
    try:
        client = _client()
        if client is None or not client.enabled:
            return
        client.emit_failure(
            model=model,
            backend=backend,
            tags=tags,
            latency_ms=latency_ms,
            error_code=error_code,
            raw_api_key=raw_api_key,
        )
    except Exception:
        logger.exception("CAS ledger failure emit failed")


def monotonic_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
