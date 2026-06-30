"""LLM utilities for grading runner."""

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Any

import litellm
from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    BadRequestError,
    ContentPolicyViolationError,
    ContextWindowExceededError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.files.main import ModelResponse
from loguru import logger
from pydantic import BaseModel

import runner.utils.litellm_patches  # noqa: F401
from runner.utils.decorators import (
    campaign_id_ctx,
    llm_attempt_ctx,
    llm_call_id_ctx,
    trajectory_batch_id_ctx,
    with_concurrency_limit,
    with_retry,
)
from runner.utils.metrics import distribution
from runner.utils.settings import get_settings

settings = get_settings()

# See agents/runner/utils/llm.py for rationale. `use_litellm_proxy` is a
# SDK-level switch that applies whether the per-call api_base points at the
# LiteLLM proxy or the gateway. Per-call URL via settings.apply_llm_target.
if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
    litellm.use_litellm_proxy = True

# One-shot routing log emitted on the first LLM call this process makes.
# Deferred to first call (not module-load) because setup_logger() calls
# logger.remove() before wiring the DD sink — module-load logs go to
# stderr only and never reach DD. See agents/runner/utils/llm.py for the
# canonical implementation.
_llm_route_logged = False


def _log_llm_route_once(workload: str | None = None) -> None:
    """Emit the routing decision exactly once per worker process.

    `workload` is the value the caller will pass to ``apply_llm_target``; we
    include it (and the resolved priority) in the bound fields so DD shows
    `@workload:grading_batch @priority:1` for filtering / aggregation. For
    non-gateway targets we still record workload but set priority=None since
    X-Priority isn't on the wire.
    """
    global _llm_route_logged
    if _llm_route_logged:
        return
    _llm_route_logged = True
    if settings.is_gateway_routed():
        target, api_base = "gateway", settings.LLM_GATEWAY_API_BASE
        priority: int | None = settings.priority_for_workload(workload)
    elif settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
        target, api_base = "litellm_proxy", settings.LITELLM_PROXY_API_BASE
        priority = None
    else:
        target, api_base = "none", None
        priority = None
    logger.bind(
        llm_route_target=target,
        llm_route_api_base=api_base,
        env=settings.ENV.value,
        runner="grading",
        workload=workload,
        priority=priority,
    ).info(
        "llm-routing selected target={} workload={} priority={} env={} api_base={}",
        target,
        workload,
        priority,
        settings.ENV.value,
        api_base,
    )


# Default concurrency limit for LLM calls
LLM_CONCURRENCY_LIMIT = 10

# Context variable for grading run ID
grading_run_id_ctx: ContextVar[str | None] = ContextVar("grading_run_id", default=None)


class CacheControlAllowlist(StrEnum):
    """Targets where we explicitly attach Anthropic-style ``cache_control``.

    Anthropic-family models *require* an explicit ``cache_control`` field to
    enable prompt caching. Every other major provider Studio routes to
    (OpenAI, Gemini direct, Vertex Gemini, OpenRouter) caches input prompts
    automatically — adding ``cache_control`` for those paths would be
    noise without benefit, so we don't bother.

    Mirrors the agents-runner allowlist in ``runner.utils.llm`` so a
    grading judge routed through the same provider sees the same caching
    behavior.
    """

    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"


_CACHE_CONTROL_ALLOWLIST: frozenset[str] = frozenset(
    member.value for member in CacheControlAllowlist
)
# Explicit 5-minute TTL. ``{"type": "ephemeral"}`` alone also defaults to 5m,
# but pinning ``ttl="5m"`` keeps us from silently inheriting any future
# change Anthropic makes to the default and makes the choice auditable
# alongside the (more expensive) ``ttl="1h"`` alternative.
_EPHEMERAL_CACHE: dict[str, str] = {"type": "ephemeral", "ttl": "5m"}


def _is_cache_control_allowed(model: str) -> bool:
    """Whether ``model`` accepts Anthropic-style ``cache_control`` markers.

    Matches when the first path segment is allowlisted (``anthropic/foo``,
    ``bedrock/foo`` — unchanged) OR when the alias namespace is ``code_data/``
    and a later segment is allowlisted (``code_data/anthropic/foo``). This
    deliberately does *not* scan every segment — ``openrouter/anthropic/foo``
    must keep skipping cache markers.
    """
    if model in _CACHE_CONTROL_ALLOWLIST:
        return True
    segments = model.split("/")
    if not segments:
        return False
    if segments[0] in _CACHE_CONTROL_ALLOWLIST:
        return True
    if segments[0] == "code_data":
        return any(seg in _CACHE_CONTROL_ALLOWLIST for seg in segments[1:])
    return False


def _with_cached_system_prompt(
    messages: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    """Return ``messages`` with the system prompt marked ephemerally cacheable.

    The grading judge typically issues one LLM call per criterion, all
    sharing the same ``GRADING_SYSTEM_PROMPT``. With 20-40 criteria per
    Balboa rubric and a 10-wide concurrency cap, the system prompt is
    re-sent dozens of times within the cache's 5-minute TTL — marking it
    cacheable lets every call after the first hit Anthropic's
    ``cache_read`` tier instead of paying full input rate.

    No-op unless :func:`_is_cache_control_allowed` returns True for ``model``. On a match, attaches
    ``cache_control={"type": "ephemeral", "ttl": "5m"}`` to the final
    content block of the first system message. Idempotent: a no-op if
    there is no system message, the system message isn't a dict, or the
    last block already carries ``cache_control``.

    Mirrors ``runner.utils.llm._with_cached_system_prompt`` in the
    agents-runner; kept as a separate copy because the two runners ship
    independently and don't share a utils package.
    """
    if not _is_cache_control_allowed(model):
        return messages

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content:
            block = {"type": "text", "text": content, "cache_control": _EPHEMERAL_CACHE}
            new_msg: dict[str, Any] = {"role": "system", "content": [block]}
            return [*messages[:i], new_msg, *messages[i + 1 :]]
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and "cache_control" not in last:
                cached_last = {**last, "cache_control": _EPHEMERAL_CACHE}
                new_msg = {**msg, "content": [*content[:-1], cached_last]}
                return [*messages[:i], new_msg, *messages[i + 1 :]]
        return messages
    return messages


def _is_non_retriable_error(e: Exception) -> bool:
    """
    Detect errors that are deterministic and should NOT be retried.

    These include:
    - Context window exceeded (content-based detection for providers that don't classify properly)
    - Configuration/validation errors that will always fail

    Note: Patterns must be specific enough to avoid matching transient errors
    like rate limits (e.g., "maximum of 100 requests" should NOT match).
    """
    error_str = str(e).lower()

    non_retriable_patterns = [
        # Context window patterns
        "token count exceeds",
        "context_length_exceeded",
        "context length exceeded",
        "maximum context length",
        "maximum number of tokens",
        "prompt is too long",
        "input too long",
        "exceeds the model's maximum context",
        # Tool count errors - be specific to avoid matching rate limits
        "tools are supported",  # "Maximum of 128 tools are supported"
        "too many tools",
        # Model/auth errors
        "model not found",
        "does not exist",
        "invalid api key",
        "authentication failed",
        "unauthorized",
        "invalid base64",
    ]

    return any(pattern in error_str for pattern in non_retriable_patterns)


@contextmanager
def grading_context(grading_run_id: str) -> Generator[None]:
    """
    Context manager for setting grading_run_id, similar to logger.contextualize().

    Usage:
        with grading_context(grading_run_id):
            # All LLM calls in here automatically get the grading_run_id in metadata
            ...
    """
    token = grading_run_id_ctx.set(grading_run_id)
    try:
        yield
    finally:
        grading_run_id_ctx.reset(token)


def build_messages(
    system_prompt: str,
    user_prompt: str,
    images: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Build messages list for LLM call.

    Args:
        system_prompt: System prompt content
        user_prompt: User prompt content
        images: Optional list of image dicts with 'url' key for vision models

    Returns:
        List of message dicts ready for LiteLLM
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    if images:
        # Build multimodal user message with text + images
        # Each image is preceded by a text label with its placeholder ID
        # so the LLM can correlate images with artifact content
        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": user_prompt},
        ]
        for img in images:
            if img.get("url"):
                # Add text label before image to identify it
                placeholder = img.get("placeholder", "")
                if placeholder:
                    user_content.append(
                        {"type": "text", "text": f"IMAGE: {placeholder}"}
                    )
                user_content.append(
                    {"type": "image_url", "image_url": {"url": img["url"]}}
                )
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_prompt})

    return messages


_ERROR_STATUS_MAP = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    408: "timeout",
    429: "rate_limit",
    504: "timeout",
}


def _classify_llm_error(exc: BaseException) -> str:
    """Bounded ``error_type`` for the ``llm.request.latency_seconds`` baseline.

    Client-side failures are matched by TYPE first: LiteLLM tags
    ``APIConnectionError`` with a synthetic 500 and ``Timeout`` with 408, and
    ``ContentPolicyViolationError`` subclasses ``BadRequestError`` (400); a
    status-first lookup would misclassify all three. Everything else keys off
    the real HTTP status code. Mirrors the studio-side classifier so studio and
    archipelago share one taxonomy.
    """
    if isinstance(exc, Timeout):
        return "timeout"
    if isinstance(exc, APIConnectionError):
        return "connection"
    if isinstance(exc, ContentPolicyViolationError):
        return "content_policy"
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _ERROR_STATUS_MAP:
            return _ERROR_STATUS_MAP[status]
        if 500 <= status < 600:
            return "server_error"
    return "other"


def _emit_llm_latency_baseline(
    *,
    model: str,
    workload: str | None,
    latency_seconds: float,
    ok: bool,
    error_type: str | None,
) -> None:
    """Emit the cross-repo ``llm.request.latency_seconds`` distribution.

    Same metric + tag schema as the studio-side emit
    (``rl-studio/server/utils/llm/main.py``), so one DD dashboard (RLS-7657)
    covers studio + archipelago; here it captures grading-batch traffic that
    never flows through studio ``call_llm``. ``priority``/``path`` are resolved
    for BOTH gateway and litellm routes (the value is computed even though
    X-Priority only goes on the wire for the gateway). ``env``/``service`` are
    supplied by ``metrics.BASE_TAGS`` (do not duplicate them). Fire-and-forget
    and defensive: instrumentation must never break a real LLM call.
    """
    try:
        priority = settings.priority_for_workload(workload)
        path = "gateway" if settings.is_gateway_routed() else "litellm"
        # Emitted per attempt (the @with_retry wrapper re-invokes call_llm), so
        # each real round-trip is a sample. `is_retry` lets consumers recover
        # per-logical-call views: count(is_retry:false) == logical calls (parity
        # with studio's once-per-call emit), while the full set is per-request.
        is_retry = llm_attempt_ctx.get() > 1
        distribution(
            "llm.request.latency_seconds",
            latency_seconds,
            tags=[
                f"priority:P{priority}",
                f"path:{path}",
                f"model:{model}",
                f"workload:{workload or 'none'}",
                f"status:{'ok' if ok else 'error'}",
                f"error_type:{error_type or 'none'}",
                f"is_retry:{'true' if is_retry else 'false'}",
            ],
        )
    except Exception:
        logger.opt(exception=True).warning("Failed to emit llm.request.latency_seconds")


@with_retry(
    max_retries=10,
    base_backoff=5,
    jitter=5,
    retry_on=(
        RateLimitError,
        Timeout,
        BadRequestError,
        ServiceUnavailableError,
        APIConnectionError,
        InternalServerError,
        BadGatewayError,
    ),
    skip_on=(ContextWindowExceededError,),
    skip_if=_is_non_retriable_error,
)
@with_concurrency_limit(max_concurrency=LLM_CONCURRENCY_LIMIT)
async def call_llm(
    model: str,
    messages: list[dict[str, Any]],
    timeout: int,
    extra_args: dict[str, Any] | None = None,
    response_format: dict[str, Any] | type[BaseModel] | None = None,
) -> ModelResponse:
    """
    Call LLM with retry logic.

    Args:
        model: Full model string (e.g., "gemini/gemini-2.0-flash")
        messages: List of message dicts (caller builds system/user/images)
        timeout: Request timeout in seconds
        extra_args: Extra LLM arguments (temperature, max_tokens, etc.)
        response_format: For structured output - {"type": "json_object"} or Pydantic class

    Returns:
        ModelResponse from LiteLLM
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": _with_cached_system_prompt(messages, model),
        "timeout": timeout,
        # Outer @with_retry owns retries — pin num_retries=0 so the LiteLLM
        # proxy doesn't retry on top, compounding caller × proxy attempts.
        # Caller's extra_args wins via the spread (later key overrides).
        "num_retries": 0,
        **(extra_args or {}),
    }

    if response_format:
        kwargs["response_format"] = response_format

    # If LiteLLM proxy is configured, route through it and ship tracking tags
    # via HTTP headers only — litellm 1.83.10 default-strips body-supplied
    # `metadata.tags` unless the key has admin metadata `allow_client_tags:
    # true` (we don't). Header tags bypass the strip via the proxy's
    # `extra_spend_tag_headers` allowlist (see rl-studio/infra/litellm/config.yaml).
    # Docs: https://docs.litellm.ai/docs/proxy/cost_tracking
    # Route via the LLM Gateway (DEV only) or LiteLLM proxy + ship
    # spend-attribution tags through extra_headers. See agents/runner/utils/llm.py
    # for the full rationale; gateway PR #32 forwards these headers upstream.
    workload = "grading_batch" if trajectory_batch_id_ctx.get() else "grading_single"
    _log_llm_route_once(workload=workload)
    settings.apply_llm_target(
        kwargs,
        fairness_key=campaign_id_ctx.get(),
        workload=workload,
    )
    grading_run_id = grading_run_id_ctx.get()
    if kwargs.get("api_base"):
        call_id = llm_call_id_ctx.get()
        attempt_num = llm_attempt_ctx.get() if call_id else None
        hdrs = dict(kwargs.get("extra_headers") or {})
        hdrs.setdefault("service", "grading")
        if grading_run_id:
            hdrs.setdefault("grading_run_id", grading_run_id)
        if call_id:
            hdrs.setdefault("call_id", call_id)
            hdrs.setdefault("attempt", str(attempt_num))
        if model.startswith("code_data/"):
            hdrs.setdefault("purpose", "code_data_eval")
        kwargs["extra_headers"] = hdrs

    start = time.perf_counter()
    ok = False
    error_type: str | None = None
    try:
        response = await litellm.acompletion(**kwargs)
        # ok flips only AFTER validation succeeds, so a parse/shape failure is
        # counted as status:error (not a false success on the baseline).
        validated = ModelResponse.model_validate(response)
        ok = True
        return validated
    except BaseException as exc:
        error_type = _classify_llm_error(exc)
        raise
    finally:
        _emit_llm_latency_baseline(
            model=model,
            workload=workload,
            latency_seconds=time.perf_counter() - start,
            ok=ok,
            error_type=error_type,
        )
