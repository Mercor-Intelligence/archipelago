"""LLM utilities for grading runner."""

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from functools import partial
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
from runner.utils import budget_meter
from runner.utils.budget_hydration import request_meter_hydration
from runner.utils.cas_ledger_emit import (
    monotonic_ms,
    record_cas_failure,
    record_cas_success,
    tags_from_spend_headers,
)
from runner.utils.decorators import (
    account_id_ctx,
    actor_user_id_ctx,
    budget_enabled_ctx,
    budget_stop_ctx,
    campaign_id_ctx,
    llm_attempt_ctx,
    llm_call_id_ctx,
    model_rates_ctx,
    task_id_ctx,
    trajectory_batch_id_ctx,
    trajectory_id_ctx,
    with_concurrency_limit,
    with_retry,
    world_id_ctx,
)
from runner.utils.metrics import distribution
from runner.utils.settings import get_settings, work_unit_ctx

# Redis client for the batch-spend guardrail meter. Defensive: some contexts don't
# configure Redis — fall back to None so the meter no-ops.
try:
    # noqa on a pre-existing line: the guarded import must stay inside the try (that
    # IS the fallback), and the alias mirrors the module-private client it wraps.
    # Flagged only because this file is now in the changed-files lint scope.
    from runner.utils.redis import redis_client as _budget_redis  # noqa: RLS001,RLS038
except (ImportError, ValueError):
    _budget_redis = None

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


def _ephemeral_cache(ttl: str) -> dict[str, str]:
    return {"type": "ephemeral", "ttl": ttl}


# Explicit 5-minute TTL. ``{"type": "ephemeral"}`` alone also defaults to 5m,
# but pinning ``ttl="5m"`` keeps us from silently inheriting any future
# change Anthropic makes to the default and makes the choice auditable
# alongside the (more expensive) ``ttl="1h"`` alternative.
_EPHEMERAL_CACHE: dict[str, str] = _ephemeral_cache("5m")

# TTLs a caller may select for the user-prompt prefix breakpoint. Anything else
# (including the empty default) leaves the split off, so an unrecognised value
# degrades to today's behaviour instead of putting a bad marker on the wire.
_USER_PREFIX_CACHE_TTLS: frozenset[str] = frozenset({"5m", "1h"})

# Shortest to longest. Anthropic requires a longer-TTL cache entry to appear
# BEFORE any shorter-TTL one, and `tools` -> `system` -> `messages` is the render
# order, so the system breakpoint can never be the shorter of the two. Ordering
# these lets `_longest_message_cache_ttl` enforce that from one place; a new TTL
# means one new entry here.
_TTL_RANK: dict[str, int] = {"5m": 0, "1h": 1}


def _user_prefix_cache_ttl() -> str:
    """The configured TTL for user-prompt prefix caching, or "" when disabled.

    Read per call rather than captured at import so the setting can be flipped
    on a running deployment. ``1h`` is the one to reach for on the criterion
    fan-out: the write costs 2x instead of 1.25x, but it pays back after three
    calls, and a 118-call fan-out at a concurrency of
    ``LLM_CONCURRENCY_LIMIT`` can outlast the 5-minute window — in which case
    5m silently degrades to paying the write premium repeatedly.

    Selecting ``1h`` also promotes the system-prompt breakpoint to 1h, because
    the shorter one may not come first — see ``_longest_message_cache_ttl``. The
    system prompt is small next to the deliverable, so the extra write premium
    on it is noise, and it wants the same lifetime regardless.
    """
    ttl = (settings.GRADING_PROMPT_CACHE_TTL or "").strip()
    return ttl if ttl in _USER_PREFIX_CACHE_TTLS else ""


def prefix_cache_enabled() -> bool:
    """Whether user-prompt prefix caching is currently on.

    Public because the verifier fan-out needs it: priming the cache is only
    worth its latency when there is a cache to prime. Reads the setting per
    call for the same reason ``_user_prefix_cache_ttl`` does.
    """
    return bool(_user_prefix_cache_ttl())


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


def _longest_cache_ttl(messages: list[dict[str, Any]], *, skip_system: bool) -> str:
    """The longest cache TTL marked on any block, else "5m".

    Two callers with different scopes: the system-prompt promotion skips the
    system message (it is deciding what to stamp there), and pricing does not
    (it needs what actually went on the wire).
    """
    longest = "5m"
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if skip_system and msg.get("role") == "system":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            ttl = (block.get("cache_control") or {}).get("ttl")
            if isinstance(ttl, str) and _TTL_RANK.get(ttl, -1) > _TTL_RANK[longest]:
                longest = ttl
    return longest


def _longest_message_cache_ttl(messages: list[dict[str, Any]]) -> str:
    """The longest cache TTL already marked on a non-system block, else "5m".

    Exists to keep the system breakpoint from being the *shorter* of two TTLs.
    Anthropic requires cache entries with a longer TTL to appear before shorter
    ones, and ``system`` renders before ``messages`` — so a 5m system breakpoint
    in front of a 1h user breakpoint is an invalid request. Deriving the system
    TTL from what the messages actually carry makes the ordering correct by
    construction rather than by convention, and it is the honest lifetime
    anyway: both blocks are cached for the same fan-out.
    """
    return _longest_cache_ttl(messages, skip_system=True)


def _with_cached_system_prompt(
    messages: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    """Return ``messages`` with the system prompt marked ephemerally cacheable.

    The grading judge typically issues one LLM call per criterion, all
    sharing the same ``GRADING_SYSTEM_PROMPT``. With 20-40 criteria per
    large criterion rubric and a 10-wide concurrency cap, the system prompt is
    re-sent dozens of times within the cache's 5-minute TTL — marking it
    cacheable lets every call after the first hit Anthropic's
    ``cache_read`` tier instead of paying full input rate.

    No-op unless :func:`_is_cache_control_allowed` returns True for ``model``. On a match, attaches
    ``cache_control={"type": "ephemeral", "ttl": <ttl>}`` to the final
    content block of the first system message, where ``<ttl>`` is 5m unless a
    later message already carries a longer one — see
    :func:`_longest_message_cache_ttl` for why it has to match or exceed it.
    Idempotent: a no-op if there is no system message, the system message isn't
    a dict, or the last block already carries ``cache_control``.

    Mirrors ``runner.utils.llm._with_cached_system_prompt`` in the
    agents-runner; kept as a separate copy because the two runners ship
    independently and don't share a utils package.
    """
    if not _is_cache_control_allowed(model):
        return messages

    cache_control = _ephemeral_cache(_longest_message_cache_ttl(messages))

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content:
            block = {"type": "text", "text": content, "cache_control": cache_control}
            new_msg: dict[str, Any] = {"role": "system", "content": [block]}
            return [*messages[:i], new_msg, *messages[i + 1 :]]
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and "cache_control" not in last:
                cached_last = {**last, "cache_control": cache_control}
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
    if isinstance(e, budget_meter.BudgetExceededError):
        return True  # terminal: a budget stop must not be retried
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


def _split_cacheable_prefix(
    user_prompt: str, cacheable_prefix: str | None, model: str
) -> tuple[str, str, str] | None:
    """Return ``(prefix, remainder, ttl)`` when the prompt should be split, else None.

    The TTL travels with the split rather than being re-read at the call site, so
    a setting flipped mid-call cannot decide to split and then stamp a marker
    from a different configuration.

    The split exists so the criterion fan-out stops paying full input rate for
    the same deliverable on every call: one grading run issues one call per
    criterion, each re-sending an identical prefix, so marking that prefix
    cacheable bills it at the ``cache_read`` tier from the second call on.

    Every guard here returns None, i.e. falls back to the single-block path that
    predates this. In particular a ``cacheable_prefix`` that is not literally a
    prefix of ``user_prompt`` is treated as absent rather than trusted — a caller
    that drifts loses the discount, it never sends a mangled prompt.
    """
    ttl = _user_prefix_cache_ttl()
    if not ttl or not cacheable_prefix:
        return None
    if not _is_cache_control_allowed(model):
        return None
    if not user_prompt.startswith(cacheable_prefix):
        logger.bind(event="prompt_cache_prefix_mismatch", model=model).warning(
            "cacheable_prefix is not a prefix of user_prompt; sending unsplit"
        )
        return None
    remainder = user_prompt[len(cacheable_prefix) :]
    # A split that leaves nothing after it would put the breakpoint at the very
    # end of the message, which caches the whole prompt including the criterion:
    # every call writes its own entry and none of them ever read one.
    if not remainder:
        return None
    return cacheable_prefix, remainder, ttl


def build_messages(
    system_prompt: str,
    user_prompt: str,
    images: list[dict[str, Any]] | None = None,
    *,
    cacheable_prefix: str | None = None,
    model: str = "",
) -> list[dict[str, Any]]:
    """
    Build messages list for LLM call.

    Args:
        system_prompt: System prompt content
        user_prompt: User prompt content — always the COMPLETE prompt
        images: Optional list of image dicts with 'url' key for vision models
        cacheable_prefix: Optional leading slice of ``user_prompt`` that is
            identical across a fan-out of calls. Purely an optimisation hint:
            when honoured, the text is emitted as two blocks with a cache
            breakpoint between them, which changes the wire framing and not one
            byte of the prompt the model reads. Requires ``model`` to identify
            the provider, and the ``GRADING_PROMPT_CACHE_TTL`` setting to be on.
        model: Model id, needed to decide whether cache markers are supported

    Returns:
        List of message dicts ready for LiteLLM
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    split = _split_cacheable_prefix(user_prompt, cacheable_prefix, model)

    if images or split:
        # Build multimodal user message with text + images
        # Each image is preceded by a text label with its placeholder ID
        # so the LLM can correlate images with artifact content
        user_content: list[dict[str, Any]] = []
        if split:
            prefix, remainder, ttl = split
            # Breakpoint on the prefix block only. Images keep their existing
            # position after the text, so they stay outside the cached span —
            # pulling them inside would mean reordering the prompt (images ahead
            # of the criterion), and a grade-affecting change does not belong in
            # a cost change. The emitted cache_read/prompt_tokens ratio says how
            # much of the payload that leaves on the table.
            user_content.append(
                {
                    "type": "text",
                    "text": prefix,
                    "cache_control": _ephemeral_cache(ttl),
                }
            )
            user_content.append({"type": "text", "text": remainder})
        else:
            user_content.append({"type": "text", "text": user_prompt})
        for img in images or []:
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


def _emit_prompt_cache_baseline(
    *, model: str, workload: str | None, response: ModelResponse | None
) -> None:
    """Emit how much of this call's prompt was served from the prompt cache.

    Three distributions on the same tag set, so one graph answers the only
    question that matters after a caching change: read / (read + creation +
    uncached) per call. A fan-out that is working shows one call with creation
    tokens and the rest reading; a ratio pinned at zero across a fan-out means
    something in the prefix is varying and the writes are pure overhead — worse
    than not caching, because a write bills above fresh input.

    Reads the same usage fields ``_actual_call_cost_usd`` already prices from,
    so the metric and the billed cost cannot disagree. Fire-and-forget and
    defensive: instrumentation must never break a real LLM call.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        if prompt == 0:
            return
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _usage_detail(details, "cached_tokens")
        creation = _usage_detail(details, "cache_creation_tokens") or int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        tags = [
            f"model:{model}",
            f"workload:{workload or 'none'}",
            f"prefix_cache_ttl:{_user_prefix_cache_ttl() or 'off'}",
        ]
        distribution("llm.request.prompt_cache_read_tokens", cached, tags=tags)
        distribution("llm.request.prompt_cache_creation_tokens", creation, tags=tags)
        distribution("llm.request.prompt_tokens", prompt, tags=tags)
    except Exception:
        logger.opt(exception=True).warning(
            "Failed to emit llm.request.prompt_cache_* distributions"
        )


# Conservative default per-token rates, used only when litellm can't price the model,
# so the estimate is never silently $0 (which would disable the cap). See
# agents/runner/utils/llm.py for rationale.
_DEFAULT_INPUT_RATE = 1e-5
_DEFAULT_OUTPUT_RATE = 3e-5


def _model_rates(model: str) -> tuple[float, float, int]:
    """(input_$/tok, output_$/tok, default_max_output_tokens) for `model`.

    The server-threaded rate card (model_rates_ctx) wins — exact even when this
    image's litellm doesn't know the model; the container's litellm table is the
    fallback; a conservative non-zero default is last so the guardrail keeps
    enforcing.
    """
    in_rate = out_rate = 0.0
    max_out = 4096
    try:
        info = litellm.get_model_info(model)
        in_rate = info.get("input_cost_per_token") or 0.0
        out_rate = info.get("output_cost_per_token") or 0.0
        max_out = info.get("max_output_tokens") or 4096
    except Exception:
        pass
    ctx = model_rates_ctx.get()
    if ctx and ctx.get("input_cost_per_token"):
        return (
            float(ctx["input_cost_per_token"]),
            float(ctx.get("output_cost_per_token") or 0.0),
            max_out,
        )
    if in_rate <= 0 and out_rate <= 0:
        logger.debug(f"budget: no rates for {model}; using default rates")
        in_rate, out_rate = _DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE
    return in_rate, out_rate, max_out


def _usage_detail(details: Any, key: str) -> int:
    """prompt_tokens_details field → int (attr or mapping; 0 when absent)."""
    if details is None:
        return 0
    value = getattr(details, key, None)
    if value is None and isinstance(details, dict):
        value = details.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _estimate_call_cost_usd(
    model: str, messages: list[dict[str, Any]], kwargs: dict[str, Any]
) -> float:
    """Pessimistic worst-case cost for one call (input tokens + max output, priced).

    Always non-zero when rates apply — never silently 0 (that would disable the cap).
    """
    in_rate, out_rate, default_max = _model_rates(model)
    try:
        in_tokens = litellm.token_counter(model=model, messages=messages)
    except Exception:
        in_tokens = max(1, len(str(messages)) // 4)
    # Honor both output-cap keys (max_tokens / max_completion_tokens) so the
    # reservation isn't under-sized (BugBot).
    max_out = (
        kwargs.get("max_tokens") or kwargs.get("max_completion_tokens") or default_max
    )
    return in_tokens * in_rate + max_out * out_rate


def _actual_call_cost_usd(
    model: str, response: ModelResponse | None, *, creation_ttl: str = "5m"
) -> float | None:
    """Real settled cost from the response's usage, cache-aware: cached reads and
    cache-writes bill at different rates than fresh input (~0.1x / ~1.25x, or
    ~2x for a 1-hour write).
    Returns ``None`` when usage is missing/unreadable so the caller can settle at
    the reserved estimate rather than $0 — real spend is never counted free (BugBot).

    ``creation_ttl`` is the TTL the request's cache markers actually carried.
    Both existing rate paths land on the 5-minute figure — ``lookup_model_rates``
    fills ``cache_creation_cost_per_token`` from litellm's
    ``cache_creation_input_token_cost``, and the fallback below is ``in_rate *
    1.25`` — so pricing a 1-hour write with either under-reports it by 37.5%
    (billed 2x, accrued 1.25x), and that figure is what reaches
    ``budget_meter.accrue``.

    The above-1h key is read when the rate card carries it, because that is the
    real number, but it cannot be relied on: only 45 of the 2703 models in
    litellm 1.84.0's price map define
    ``cache_creation_input_token_cost_above_1hr``, and ``claude-opus-5`` is not
    in that map at all. So the ratio is the fallback. It is exactly 2.00x on
    every Anthropic model in the map that does define it.

    Assumes one TTL per request, which holds because
    ``_with_cached_system_prompt`` promotes the system block to match the
    messages. A caller that deliberately mixed TTLs would need Anthropic's
    per-bucket ``ephemeral_5m``/``ephemeral_1h`` creation split instead.
    """
    if response is None:
        return None
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        in_rate, out_rate, _ = _model_rates(model)
        ctx = model_rates_ctx.get() or {}
        cached_rate = float(ctx.get("cached_input_cost_per_token") or in_rate * 0.1)
        if creation_ttl == "1h":
            creation_rate = float(
                ctx.get("cache_creation_cost_per_token_above_1h") or in_rate * 2.0
            )
        else:
            creation_rate = float(
                ctx.get("cache_creation_cost_per_token") or in_rate * 1.25
            )
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        if prompt == 0 and completion == 0:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _usage_detail(details, "cached_tokens")
        creation = _usage_detail(details, "cache_creation_tokens") or int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        uncached = max(prompt - cached - creation, 0)
        return (
            uncached * in_rate
            + cached * cached_rate
            + creation * creation_rate
            + completion * out_rate
        )
    except Exception:
        return None


@with_retry(
    max_retries=5,
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
        # Pin SDK-level retries to 0 — outer @with_retry owns all retry
        # logic for archipelago. Proxy is also pass-through (num_retries: 0).
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
    # None when no proxy/gateway is configured — the finally block below only
    # emits to CAS when this is set, since there's nothing to tag otherwise.
    hdrs: dict[str, str] | None = None
    if kwargs.get("api_base"):
        call_id = llm_call_id_ctx.get()
        attempt_num = llm_attempt_ctx.get() if call_id else None
        hdrs = dict(kwargs.get("extra_headers") or {})
        hdrs.setdefault("service", "grading")
        # workload is already resolved above for gateway priority routing;
        # surfacing it here too costs nothing and gives CAS the same
        # grading_single/grading_batch slice Datadog already has.
        hdrs.setdefault("workload", workload)
        # work_unit is already resolved for the gateway's X-Work-Unit
        # queue-monitor header (settings.apply_llm_target, above); surfacing
        # the same value here gives CAS per-unit drill-down too.
        if work_unit := work_unit_ctx.get():
            hdrs.setdefault("work_unit", work_unit)
        if grading_run_id:
            hdrs.setdefault("grading_run_id", grading_run_id)
        # Spend-attribution tags for the Spend V2 dashboard: campaign / world /
        # trajectory ids let grading LiteLLM cost rows be sliced per-campaign,
        # per-project (world), and per-trajectory. All three are already in the
        # proxy's `extra_spend_tag_headers` allowlist (rl-studio/infra/litellm/
        # config.yaml); each is threaded via a ContextVar set in
        # `modal_labs.run_grading`. Omitted when unset (older server / task-scoped
        # verifier with no world). `setdefault` keeps any caller-supplied value.
        campaign_id = campaign_id_ctx.get()
        if campaign_id:
            hdrs.setdefault("campaign_id", campaign_id)
        # task_id: the graded trajectory's owning task, threaded onto
        # task_id_ctx at the grading worker entrypoint (modal_labs.run_grading)
        # from GradingConfig.task_id — same universal-key attribution
        # trajectory/autoqc/synthetic already emit for CAS's task_id
        # drill-down. None (older server, or a trajectory with no owning
        # task) omits the header.
        task_id = task_id_ctx.get()
        if task_id:
            hdrs.setdefault("task_id", task_id)
        account_id = account_id_ctx.get()
        if account_id:
            hdrs.setdefault("account_id", account_id)
        actor_user_id = actor_user_id_ctx.get()
        if actor_user_id:
            hdrs.setdefault("user_id", actor_user_id)
        world_id = world_id_ctx.get()
        if world_id:
            hdrs.setdefault("world_id", world_id)
        trajectory_id = trajectory_id_ctx.get()
        if trajectory_id:
            hdrs.setdefault("trajectory_id", trajectory_id)
        # batch_id: same ContextVar workload above already reads to pick
        # grading_batch vs grading_single — just not previously surfaced as
        # its own tag. None for one-off (non-batch) grading runs.
        batch_id = trajectory_batch_id_ctx.get()
        if batch_id:
            hdrs.setdefault("batch_id", batch_id)
        if call_id:
            hdrs.setdefault("call_id", call_id)
            hdrs.setdefault("attempt", str(attempt_num))
        if model.startswith("code_data/"):
            hdrs.setdefault("purpose", "code_data_eval")
        kwargs["extra_headers"] = hdrs

    # Batch-spend guardrail: gate when over budget, accrue actual in `finally`.
    # Inert unless enabled; fail-open on any Redis trouble.
    budget_unit = trajectory_batch_id_ctx.get() if budget_enabled_ctx.get() else None
    budget_call_id = f"{llm_call_id_ctx.get()}:{llm_attempt_ctx.get()}"
    budget_est = 0.0
    if budget_unit is not None:
        budget_est = _estimate_call_cost_usd(model, kwargs["messages"], kwargs)
        snap = await budget_meter.read_state(
            _budget_redis, budget_unit, lanes=budget_meter.BATCH_LANES
        )
        # Redis lost this unit's keys, so the gate below would read "no cap" as
        # "unenforced" and admit everything. Ask the server to rebuild from the durable
        # mirror — lock-deduped across the fan-out, so hundreds of workers produce one
        # request. Never blocks and never raises: THIS call proceeds ungated (the pre-call
        # gate's inherent one-call exposure) and the NEXT one is gated again.
        await budget_meter.hydrate_if_stale(
            _budget_redis,
            budget_unit,
            snap,
            partial(request_meter_hydration, budget_unit),
        )
        if snap and snap.remaining_usd is not None and snap.remaining_usd <= 0:
            budget_stop_ctx.set(
                {
                    "reason": "budget_exceeded",
                    "cost_unit": budget_unit,
                    "remaining_usd": snap.remaining_usd,
                    "model": model,
                }
            )
            logger.bind(
                event="budget_call_denied", budget_unit=budget_unit, model=model
            ).warning(f"Judge LLM call denied: batch {budget_unit} over budget")
            raise budget_meter.BudgetExceededError(
                budget_unit, remaining_usd=snap.remaining_usd, model=model
            )

    response_obj: ModelResponse | None = None
    start = time.perf_counter()
    ok = False
    error_type: str | None = None
    try:
        # noqa on a pre-existing line: this function IS the grading runner's spend-tracking
        # wrapper, so it is the one place that must call litellm directly. Flagged only
        # because this file entered the changed-files lint scope.
        response = await litellm.acompletion(**kwargs)  # noqa: RLS014
        # ok flips only AFTER validation succeeds, so a parse/shape failure is
        # counted as status:error (not a false success on the baseline).
        validated = ModelResponse.model_validate(response)
        response_obj = validated
        ok = True
        return validated
    except BaseException as exc:
        error_type = _classify_llm_error(exc)
        raise
    finally:
        if budget_unit is not None:
            # Unreadable usage on success → $0 real cost + estimate to the untracked
            # side ledger (DD-alertable).
            #
            # The TTL is read off the messages that actually went on the wire, not
            # from the setting: the setting can be flipped mid-call, and a
            # non-Anthropic model carries no markers at all. A 1-hour write bills
            # 2x where the default rate path assumes 1.25x, so getting this wrong
            # under-reports every write token by 37.5% into the meter.
            _cost = (
                _actual_call_cost_usd(
                    model,
                    response_obj,
                    creation_ttl=_longest_cache_ttl(
                        kwargs["messages"], skip_system=False
                    ),
                )
                if ok
                else 0.0
            )
            untracked_est = 0.0
            if ok and _cost is None:
                logger.bind(
                    event="llm_usage_missing_estimate_fallback",
                    model=model,
                    budget_call_id=budget_call_id,
                    estimated_usd=budget_est,
                ).warning(
                    f"LLM usage unreadable on a successful call; counted $0 real cost, "
                    f"${budget_est:.6f} to the untracked ledger (model={model})"
                )
                untracked_est = budget_est
            await budget_meter.accrue(
                _budget_redis,
                budget_unit,
                budget_call_id,
                _cost or 0.0,
                budget_meter.LANE_GRADING,
                untracked_est_usd=untracked_est,
            )
        _emit_llm_latency_baseline(
            model=model,
            workload=workload,
            latency_seconds=time.perf_counter() - start,
            ok=ok,
            error_type=error_type,
        )
        _emit_prompt_cache_baseline(
            model=model, workload=workload, response=response_obj
        )
        # CAS ledger emit — this function already builds a complete,
        # correct spend-attribution header set (service=grading,
        # grading_run_id, trajectory_id, batch_id, world_id, campaign_id,
        # account_id, user_id, workload, work_unit, purpose) sent to the
        # proxy via extra_headers, but never told CAS's ledger about the
        # call — that data reached the proxy's own spend log / Datadog and
        # nothing else. hdrs is None when no proxy/gateway is configured,
        # in which case there's nothing to emit (mirrors
        # archipelago/agents/runner/utils/llm.py's identical fix).
        if hdrs is not None:
            cas_tags = tags_from_spend_headers(hdrs)
            cas_latency_ms = monotonic_ms(start)
            if ok and response_obj is not None:
                record_cas_success(
                    response_obj,
                    model=model,
                    tags=cas_tags,
                    backend="litellm",
                    latency_ms=cas_latency_ms,
                    raw_api_key=kwargs.get("api_key"),
                )
            else:
                record_cas_failure(
                    model=model,
                    tags=cas_tags,
                    backend="litellm",
                    latency_ms=cas_latency_ms,
                    error_code=error_type,
                    raw_api_key=kwargs.get("api_key"),
                )
