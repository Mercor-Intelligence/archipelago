"""Best-effort CAS ledger ingest for archipelago agent LLM call sites.

Soft-imports ``mercor_cas_client`` (optional in island deliveries / slim test
envs). Never raises into the LLM call path.
"""

from __future__ import annotations

import time
from functools import lru_cache
from types import SimpleNamespace
from typing import Any, Literal

from loguru import logger

try:
    from mercor_cas_client import CasClient, CasClientConfig
except ImportError:  # island deliveries skip the private package
    CasClient = None  # type: ignore[assignment,misc]
    CasClientConfig = None  # type: ignore[assignment,misc]

BackendLiteral = Literal["litellm", "direct"]

# Studio CAS schema v1 keys that agent spend headers already carry. Keep
# call_id / attempt out — they are correlation headers, not ledger tags.
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
        return {"service": "trajectory", "campaign_id": _PLACEHOLDER_CAMPAIGN}
    tags = {k: str(v) for k, v in headers.items() if k in _TAG_KEYS and v}
    tags.setdefault("service", "trajectory")
    tags.setdefault("campaign_id", _PLACEHOLDER_CAMPAIGN)
    return tags


def bedrock_converse_to_cas_response(
    body: dict[str, Any],
    *,
    model: str,
) -> Any:
    """Adapt Bedrock Converse JSON into a LiteLLM-shaped carrier for CAS extract.

    ``emit_from_litellm_response`` expects ``response.usage.prompt_tokens`` /
    ``completion_tokens`` (or ``input_tokens`` / ``output_tokens``). Bedrock
    Converse returns camelCase ``inputTokens`` / ``outputTokens`` on a plain
    dict, which ``getattr``-based extraction cannot read.
    """
    usage_raw = body.get("usage") if isinstance(body, dict) else None
    prompt_tokens = 0
    completion_tokens = 0
    if isinstance(usage_raw, dict):
        try:
            prompt_tokens = int(
                usage_raw.get("inputTokens")
                or usage_raw.get("input_tokens")
                or usage_raw.get("prompt_tokens")
                or 0
            )
        except (TypeError, ValueError):
            prompt_tokens = 0
        try:
            completion_tokens = int(
                usage_raw.get("outputTokens")
                or usage_raw.get("output_tokens")
                or usage_raw.get("completion_tokens")
                or 0
            )
        except (TypeError, ValueError):
            completion_tokens = 0
    request_id: str | None = None
    metadata = body.get("$metadata") if isinstance(body, dict) else None
    if isinstance(metadata, dict):
        rid = metadata.get("requestId")
        if isinstance(rid, str) and rid:
            request_id = rid
    return SimpleNamespace(
        model=model,
        id=request_id,
        usage=SimpleNamespace(
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
        ),
    )


def correlation_from_spend_headers(
    headers: dict[str, str] | None,
) -> tuple[str | None, int | None]:
    """Pull the correlation pair out of spend headers as CAS field types.

    ``call_id`` / ``attempt`` are deliberately absent from ``_TAG_KEYS`` — they
    are first-class event fields, not ledger tags. They are what lets a CAS
    event join to the gateway spendlog row for the same HTTP attempt, so the
    failure path has to forward them rather than drop them with the tags.
    """
    if not headers:
        return None, None
    call_id = headers.get("call_id") or None
    try:
        attempt = int(headers["attempt"]) if headers.get("attempt") else None
    except (TypeError, ValueError):
        attempt = None
    return call_id, attempt


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
    call_id: str | None = None,
    attempt: int | None = None,
    raw_api_key: str | None = None,
) -> None:
    """Record an attempt that raised.

    The gateway meters per HTTP attempt, so a call that fails twice before
    succeeding is three spendlog rows. Emitting only on success left this
    producer structurally short of the gateway on every retried call, and left
    the failure itself unaccounted: a retry storm and a quiet day look
    identical in the ledger.

    A failed attempt usually carries no usage, so this mostly buys event
    parity rather than dollars — but the attempts that do bill (a timeout
    after the prompt is already prefilled) are billed by the provider whether
    or not we record them.
    """
    client: Any | None = None
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
            call_id=call_id,
            attempt=attempt,
            raw_api_key=raw_api_key,
        )
    except Exception:
        logger.exception("CAS ledger failure emit failed; preserving LLM semantics")
        try:
            if client is not None and getattr(client, "enabled", False):
                client.record_emission_failure()
        except Exception:
            logger.exception("Failed to count CAS emission failure")


def flush_cas_ledger(timeout_s: float = 5.0) -> None:
    """Drain buffered events, and account for whatever did not make it.

    Two reasons this cannot be left to the client's own atexit hook. The flush
    thread is a daemon, and ``atexit`` does not run when a process is
    signalled — which is the ordinary way an agent pod ends (SIGTERM on
    eviction or preemption, SIGKILL after the grace period). Anything still
    queued at that moment is lost with no trace.

    The drop counters are the other half: the client counts every event it
    discards and why, but only a caller that asks ever sees them. Logging the
    snapshot here is what makes client-side loss visible at all.
    """
    client = _client()
    if client is None or not client.enabled:
        return
    try:
        delivered = client.flush(timeout_s=timeout_s)
        drops = {reason: n for reason, n in client.drops.snapshot().items() if n}
        if not delivered or drops:
            logger.warning(
                f"CAS ledger flush incomplete: delivered={delivered} drops={drops}"
            )
    except Exception:
        logger.exception("CAS ledger flush failed")


def monotonic_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
