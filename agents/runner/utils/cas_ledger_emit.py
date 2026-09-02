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
    from mercor_cas_client import (
        CasClient,
        CasClientConfig,
        encrypt_key_hint,
        fingerprint_credential_id,
    )
except ImportError:  # island deliveries skip the private package
    CasClient = None  # type: ignore[assignment,misc]
    CasClientConfig = None  # type: ignore[assignment,misc]
    encrypt_key_hint = None  # type: ignore[assignment]
    fingerprint_credential_id = None  # type: ignore[assignment]

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


def shared_credential_fields(raw_key: str | None) -> dict[str, str | None]:
    """Both CAS credential fields for a shared gateway/proxy credential.

    Returns ``{"credential_id": ..., "key_hint_cipher": ...}``, both derived
    from the SAME raw key, so the two can never disagree about which credential
    a row describes. Parity is enforced here rather than at each call site: the
    columns are only jointly meaningful (the id is what you ``GROUP BY``, the
    cipher is what lets a resolver later NAME that id), and a row carrying one
    without the other is a resolver gap that looks like data.

    ``key_hint_cipher`` is a reversible, asymmetrically-encrypted copy of the
    key, decryptable only by whoever separately holds the matching private key
    -- which nothing in this process ever has. It has been populated in
    production since 2026-08-23; emitting it for a shared gateway credential is
    deliberate parity with that, not a new exposure. Splat into
    ``record_cas_success``/``record_cas_failure``.

    Note this is a COARSER identity than the per-deployment credential the
    proxy resolves server-side and stamps as ``x-cas-credential-id``: every
    call through one gateway lane fingerprints identically. That remains an
    accepted trade -- the alternative it was weighed against is NULL, not a
    finer credential.

    Empty dict when nothing is resolvable, so a splat adds no kwargs at all
    rather than explicitly writing NULLs. That includes a missing
    ``CAS_KEY_HMAC_SALT``, which is logged once by the fingerprint step.

    Never raises. Both derivations document that themselves, but this is called
    from a ``finally`` block that owes the LLM call a soft failure, and OUTSIDE
    the emitters' own try/except -- so the guarantee has to hold here rather
    than being inherited from a pinned dependency. An attribution helper must
    never be able to replace a successful response or mask the original error.
    """
    if fingerprint_credential_id is None or not raw_key:
        return {}
    try:
        credential_id = fingerprint_credential_id(raw_key)
        if credential_id is None:
            _warn_missing_hmac_salt()
            return {}
        cipher = encrypt_key_hint(raw_key) if encrypt_key_hint is not None else None
        return {"credential_id": credential_id, "key_hint_cipher": cipher}
    except Exception:
        _warn_credential_derivation_failed()
        return {}


@lru_cache(maxsize=1)
def _warn_credential_derivation_failed() -> None:
    """Log a derivation failure once per process (never the key, never the exc).

    Deliberately not ``logger.exception``: this sits on a per-call emit path,
    and the traceback could carry the raw key through a frame local.
    """
    logger.warning(
        "CAS credential derivation raised; emitting the event without "
        "credential_id/key_hint_cipher. Attribution is degraded, the LLM call "
        "itself is unaffected."
    )


@lru_cache(maxsize=1)
def _warn_missing_hmac_salt() -> None:
    """Log the missing-salt diagnostic once per process (never the key)."""
    logger.warning(
        "CAS_KEY_HMAC_SALT is unset; credential_id will be omitted from ledger "
        "rows even though a paying credential was resolved. Attribution will "
        "look identical to having no credential at all until the salt is "
        "provisioned."
    )


def record_cas_success(
    response: Any,
    *,
    model: str,
    tags: dict[str, str],
    backend: BackendLiteral = "litellm",
    latency_ms: int | None = None,
    raw_api_key: str | None = None,
    credential_id: str | None = None,
    key_hint_cipher: str | None = None,
) -> None:
    """``credential_id``/``key_hint_cipher`` must describe the same credential.

    Pass either ``raw_api_key`` (the client derives both from it,
    client.py:130-133) or the pre-derived pair from
    :func:`shared_credential_fields` -- not a mix, or a row can name one
    credential in the fingerprint column and another in the cipher.
    Callers here use the pair so a missing ``CAS_KEY_HMAC_SALT`` is logged
    rather than silently yielding a NULL fingerprint.
    """
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
            credential_id=credential_id,
            key_hint_cipher=key_hint_cipher,
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
    credential_id: str | None = None,
    key_hint_cipher: str | None = None,
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
            credential_id=credential_id,
            key_hint_cipher=key_hint_cipher,
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
