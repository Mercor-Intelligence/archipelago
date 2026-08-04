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
from runner.utils.decorators import (
    budget_enabled_ctx,
    budget_stop_ctx,
    campaign_id_ctx,
    llm_attempt_ctx,
    llm_call_id_ctx,
    model_rates_ctx,
    trajectory_batch_id_ctx,
    trajectory_id_ctx,
    with_concurrency_limit,
    with_retry,
    world_id_ctx,
)
from runner.utils.metrics import distribution
from runner.utils.settings import get_settings

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
    large criterion rubric and a 10-wide concurrency cap, the system prompt is
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


def _actual_call_cost_usd(model: str, response: ModelResponse | None) -> float | None:
    """Real settled cost from the response's usage, cache-aware: cached reads and
    cache-writes bill at different rates than fresh input (~0.1x / ~1.25x).
    Returns ``None`` when usage is missing/unreadable so the caller can settle at
    the reserved estimate rather than $0 — real spend is never counted free (BugBot).
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
        world_id = world_id_ctx.get()
        if world_id:
            hdrs.setdefault("world_id", world_id)
        trajectory_id = trajectory_id_ctx.get()
        if trajectory_id:
            hdrs.setdefault("trajectory_id", trajectory_id)
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
        snap = await budget_meter.read_state(_budget_redis, budget_unit)
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
            _cost = _actual_call_cost_usd(model, response_obj) if ok else 0.0
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
