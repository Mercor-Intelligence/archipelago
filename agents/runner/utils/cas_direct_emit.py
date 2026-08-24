"""Best-effort CAS emission for direct-provider LLM calls (no LiteLLM proxy).

Used by ``anthropic_direct`` workaround agents that call ``AsyncAnthropic``
directly. Converts the LiteLLM-shaped ``ModelResponse`` carrier and records
spend when ``CAS_TENANT`` / ``CAS_SOURCE`` are configured.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from loguru import logger

try:
    from mercor_cas_client import CasClient, CasClientConfig
except ImportError:  # island deliveries skip the private package
    CasClient = None  # type: ignore[assignment,misc]
    CasClientConfig = None  # type: ignore[assignment,misc]

from runner.utils.decorators import (
    account_id_ctx,
    actor_user_id_ctx,
    campaign_id_ctx,
    task_id_ctx,
    trajectory_batch_id_ctx,
)

_PLACEHOLDER_CAMPAIGN = "no-campaign"
_SERVICE = "trajectory"


@lru_cache(maxsize=1)
def _cas_client() -> Any | None:
    if CasClient is None or CasClientConfig is None:
        return None
    try:
        config = CasClientConfig.from_env()
        if not config.enabled:
            return None
        return CasClient(config)
    except Exception:
        logger.exception("CAS client init failed; direct emit disabled")
        return None


def _spend_tags() -> dict[str, str]:
    tags: dict[str, str] = {
        "service": _SERVICE,
        "campaign_id": (campaign_id_ctx.get() or "").strip() or _PLACEHOLDER_CAMPAIGN,
    }
    workload = (
        "trajectory_batch" if trajectory_batch_id_ctx.get() else "trajectory_single"
    )
    tags["workload"] = workload
    if acct := account_id_ctx.get():
        tags["account_id"] = acct
    if uid := actor_user_id_ctx.get():
        tags["user_id"] = uid
    if task_id := task_id_ctx.get():
        tags["task_id"] = task_id
    return tags


def record_direct_cas_success(
    response: Any,
    *,
    model: str,
    latency_ms: int,
    raw_api_key: str | None = None,
) -> None:
    client: Any | None = None
    try:
        client = _cas_client()
        if client is None or not client.enabled:
            return
        client.emit_from_litellm_response(
            response,
            model=model,
            backend="direct",
            status="success",
            tags=_spend_tags(),
            latency_ms=latency_ms,
            raw_api_key=raw_api_key,
        )
    except Exception:
        logger.exception("CAS direct emit failed; preserving agent semantics")
        try:
            if client is not None and getattr(client, "enabled", False):
                client.record_emission_failure()
        except Exception:
            logger.exception("Failed to count CAS emission failure")


def record_direct_cas_failure(
    *,
    model: str,
    latency_ms: int,
    error_code: str | None = None,
    raw_api_key: str | None = None,
) -> None:
    try:
        client = _cas_client()
        if client is None or not client.enabled:
            return
        client.emit_failure(
            model=model,
            backend="direct",
            tags=_spend_tags(),
            latency_ms=latency_ms,
            error_code=error_code,
            raw_api_key=raw_api_key,
        )
    except Exception:
        logger.exception("CAS direct failure emit failed")
