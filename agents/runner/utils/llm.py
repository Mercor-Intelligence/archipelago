"""LLM utilities for agents using LiteLLM."""

import contextlib
import json
import time
from enum import StrEnum
from functools import partial
from typing import Any

import litellm
from litellm import acompletion, aresponses, get_model_info, token_counter
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
from litellm.types.utils import Message
from loguru import logger
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

import runner.utils.litellm_patches  # noqa: F401
from runner.agents.models import LitellmAnyMessage, get_msg_attr
from runner.utils import budget_meter
from runner.utils.budget_hydration import request_meter_hydration
from runner.utils.cas_ledger_emit import (
    correlation_from_spend_headers,
    record_cas_failure,
    record_cas_success,
    shared_credential_fields,
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
    llm_max_retries_ctx,
    model_rates_ctx,
    task_id_ctx,
    trajectory_batch_id_ctx,
    trajectory_id_ctx,
    triggered_by_ctx,
    with_retry,
    world_id_ctx,
)
from runner.utils.image_fetch import (
    apply_anthropic_image_policy,
    apply_non_anthropic_image_policy,
    apply_remote_image_fetch_policy,
)
from runner.utils.metrics import distribution
from runner.utils.settings import get_settings, work_unit_ctx

# Redis client for the batch-spend guardrail meter. Defensive: some contexts (tests,
# standalone runs) don't configure Redis — fall back to None so the meter no-ops.
try:
    # noqa on a pre-existing line: the guarded import must stay inside the try (that
    # IS the fallback), and the alias mirrors the module-private client it wraps.
    # Flagged only because this file is now in the changed-files lint scope.
    from runner.utils.redis import redis_client as _budget_redis  # noqa: RLS001,RLS038
except (ImportError, ValueError):
    _budget_redis = None

settings = get_settings()

# Configure LiteLLM proxy routing if configured. `use_litellm_proxy` is a
# SDK-level switch — applies whether the per-call api_base points at the
# LiteLLM proxy directly or at the LLM Gateway in front of it (both are
# OpenAI-compatible). Per-call URL is wired via settings.apply_llm_target.
if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
    litellm.use_litellm_proxy = True


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
    covers studio + archipelago; here it captures trajectory traffic that never
    flows through studio ``call_llm``. ``priority``/``path`` are resolved for
    BOTH gateway and litellm routes (the value is computed even though
    X-Priority only goes on the wire for the gateway). ``env``/``service`` are
    supplied by ``metrics.BASE_TAGS`` (do not duplicate them). Fire-and-forget
    and defensive: instrumentation must never break a real LLM call.
    """
    try:
        priority = settings.priority_for_workload(workload)
        path = "gateway" if settings.is_gateway_routed() else "litellm"
        # Emitted per attempt (the @with_retry wrapper re-invokes the call), so
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


# One-shot routing log emitted on the first LLM call this process makes.
# Deliberately NOT at module-load time — `setup_logger()` calls
# `logger.remove()` before adding the DD sink, so anything emitted before
# that wipe goes to stderr only and never reaches DD. Deferring to the
# first call_llm gives the DD sink time to be wired and makes the choice
# visible as `@llm_route_target:gateway / :litellm_proxy / :none` in DD.
_llm_route_logged = False


def _log_llm_route_once(workload: str | None = None) -> None:
    """Emit the routing decision exactly once per worker process.

    Modal trajectory workers run with single_use_containers=True, so once-per-
    process is effectively once-per-trajectory — the workload captured here is
    representative of the trajectory's entire LLM activity.

    `workload` is the value the caller will pass to ``apply_llm_target``; we
    include it (and the resolved priority) in the bound fields so DD shows
    `@workload:trajectory_batch @priority:1` for filtering / aggregation.
    For non-gateway targets we still record workload but set priority=None
    since X-Priority isn't on the wire.
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
        runner="agents",
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


# Params that must be sent through extra_body so LiteLLM's proxy client does not
# drop them. LiteLLMProxyChatConfig._map_openai_params silently filters out any
# top-level kwarg that is not in OPENAI_CHAT_COMPLETION_PARAMS, which strips
# vendor-specific flags like `chat_template_kwargs` (Qwen/Nemotron thinking
# controls) and `include_server_side_tool_invocations` (Gemini tool-context
# circulation) before they can reach the proxy.
_EXTRA_BODY_PASSTHROUGH_KEYS = frozenset(
    {"chat_template_kwargs", "include_server_side_tool_invocations"}
)


def _extract_psf_reasoning_delta(chunk: Any) -> str:
    """Pull intermediate-output text out of a chunk's `provider_specific_fields`.

    Deliberately narrow: only reads `provider_specific_fields["reasoning_content"]`,
    never `delta.reasoning_content`. This scopes per-delta streaming logs to
    external-agent-harness providers — today just Gemini Deep Research, where
    `GenericStreamingChunk` has no first-class reasoning field so the harness
    surfaces its progress narrations via `provider_specific_fields` instead.

    The resulting logs are tagged `message_type="intermediate_output"` (not
    `"reasoning"`) because these aren't model-internal chain-of-thought —
    they're progress updates the harness emits while it's working (search
    plans, section headings, milestone narrations). The UI renders them
    with a distinct badge so viewers can tell them apart from genuine
    chain-of-thought reasoning from R1 / o-series / Anthropic thinking
    (which arrive on `delta.reasoning_content` and are logged once as
    `"reasoning"` at end-of-call by singleshot_agent).

    The field is kept as `reasoning_content` on the wire because that's
    litellm's generic channel for "non-final text"; only the downstream
    `message_type` tag distinguishes intermediate outputs from reasoning.
    """
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return ""
    psf = getattr(delta, "provider_specific_fields", None)
    if isinstance(psf, dict):
        reasoning = psf.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
    return ""


def responses_args_to_completions(extra_args: dict[str, Any]) -> dict[str, Any]:
    """Convert Responses API extra_args to Chat Completions API equivalents.

    The Responses API uses ``{"reasoning": {"effort": "high", ...}}`` while
    Chat Completions uses ``{"reasoning_effort": "high"}`` as a top-level
    param. Sending the Responses-API shape to a Chat-Completions endpoint
    yields ``Unknown parameter: 'reasoning'`` 400s.
    """
    result = {k: v for k, v in extra_args.items() if k != "reasoning"}
    reasoning = extra_args.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort and "reasoning_effort" not in result:
            result["reasoning_effort"] = effort
    return result


def _split_extra_args(
    extra_args: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split extra_args into (top_level, extra_body) for proxy-safe transport."""
    top_level: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    for k, v in extra_args.items():
        if k in _EXTRA_BODY_PASSTHROUGH_KEYS:
            extra_body[k] = v
        else:
            top_level[k] = v
    return top_level, extra_body


def _merge_extra_body(kwargs: dict[str, Any], extra_body: dict[str, Any]) -> None:
    """Merge, so apply_llm_target's forwarded `timeout` survives (RLS-10538)."""
    if not extra_body:
        return
    merged = dict(kwargs.get("extra_body") or {})
    merged.update(extra_body)
    kwargs["extra_body"] = merged


class CacheControlAllowlist(StrEnum):
    """Targets where we explicitly attach Anthropic-style ``cache_control``.

    Anthropic-family models *require* an explicit ``cache_control`` field to
    enable prompt caching. Every other major provider Studio routes to
    (OpenAI, Gemini direct, Vertex Gemini, OpenRouter) caches input prompts
    automatically — adding ``cache_control`` for those paths would be
    noise without benefit, so we don't bother.

    Members may be either:
    - A LiteLLM provider prefix (matched against the ``provider/`` segment
      of the model arg): enables caching for every model routed through
      that provider.
    - A full ``provider/model`` string (matched verbatim against the
      ``model`` argument): enables caching for that specific model only.
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


# Fireworks routes: the direct provider (``fireworks_ai/*``), the numbered
# eval/isolation lanes (``fireworks_1/*``..``fireworks_5/*``), and the vanity
# deployment aliases (``fireworks-glm-5p2/*`` etc.) all begin with this prefix.
_FIREWORKS_MODEL_PREFIX = "fireworks"


def _is_fireworks_model(model: str) -> bool:
    """Whether ``model`` routes to the Fireworks provider.

    LiteLLM's ``fireworks_ai`` provider declares ``tool_choice`` unsupported and
    400s the whole request before it ever reaches Fireworks — for *any*
    ``tool_choice`` value, ``"auto"`` included. So we must strip the key here
    rather than downgrade it. Deliberately does not touch Vercel-primary
    aliases (``alibaba/*``, ``zai/*``) whose primary lane *does* accept
    ``tool_choice`` and whose Fireworks fallback lane already drops it.
    """
    return model.lower().startswith(_FIREWORKS_MODEL_PREFIX)


# Provider segments that route to Google's Gemini API. Mirrors
# REMOTE_IMAGE_FETCH_CAPS in image_fetch.py, which enumerates the same three.
_GEMINI_PROVIDER_SEGMENTS = frozenset({"gemini", "vertex_ai", "vertex_ai_beta"})

# Non-Google families Vertex Model Garden also serves. A trailing assistant turn
# is a legitimate *prefill* for these, so they must not be caught by the Gemini
# request-shape guard — matching `vertex_ai/claude-*` here would silently break
# prefills that work today.
_VERTEX_NON_GEMINI_MARKERS = (
    "claude",
    "llama",
    "mistral",
    "jamba",
    "qwen",
    "deepseek",
)


def _is_gemini_model(model: str) -> bool:
    """Whether ``model`` routes to Google's Gemini API.

    Matched on the *provider segments* rather than a prefix: Studio assembles
    model strings with an account/lane prefix (``code_data/vertex_ai/
    gemini-3.5-flash``), so ``startswith("vertex_ai/")`` misses real routes.
    Same reasoning — and the same ``split("/")[:-1]`` shape — as
    ``resolve_remote_image_fetch_cap``.

    Covers internal codename checkpoints (``gemini/ajax-with-tool-retrieval``),
    which carry no ``gemini`` substring in the model name itself, and excludes
    the non-Google families Vertex also hosts. Fail-safe direction: a miss
    leaves the provider 400 visible, whereas a false positive would silently
    rewrite a valid Anthropic prefill.
    """
    low = model.lower()
    segments = low.split("/")[:-1]
    if not any(seg in _GEMINI_PROVIDER_SEGMENTS for seg in segments):
        return False
    return not any(marker in low for marker in _VERTEX_NON_GEMINI_MARKERS)


def _normalize_tool_call_arguments(messages: list[LitellmAnyMessage]) -> None:
    """Reparse JSON-string tool_call arguments as objects, in place.

    Tinker hands them to Nemotron's template unparsed, so the spec's JSON string
    400s once tool history exists. Opt in per orchestrator via
    ``object_tool_call_arguments``; anything unparseable is left for the provider.
    """
    for message in messages:
        for call in get_msg_attr(message, "tool_calls") or []:
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                call.function.arguments = json.loads(call.function.arguments or "{}")


def _is_empty_text_block(block: Any) -> bool:
    """Whether ``block`` is a text block with empty/whitespace-only text.

    Anthropic rejects ``cache_control`` on empty text blocks
    (``"cache_control cannot be set for empty text blocks"``), so the cache
    helpers must never mark one. A ``cache_control`` marker also forces
    LiteLLM to keep the otherwise-droppable empty block in the request, which
    is what surfaces the 400. Non-text blocks (images, documents, tool_use,
    tool_result) are never treated as empty here.
    """
    return block.get("type") == "text" and not (block.get("text") or "").strip()


def _content_is_empty(content: Any) -> bool:
    """Whether a message's ``content`` carries no usable text or blocks.

    True for ``None``, empty/whitespace-only strings, an empty list, or a list
    whose every block is an empty text block. False if any non-text block
    (image, document, tool_use, tool_result) is present.
    """
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return all(isinstance(b, dict) and _is_empty_text_block(b) for b in content)
    return False


def _with_cached_system_prompt(
    messages: list[LitellmAnyMessage], model: str
) -> list[LitellmAnyMessage]:
    """Return ``messages`` with the system prompt marked ephemerally cacheable.

    No-op unless :func:`_is_cache_control_allowed` returns True for ``model``. On a match, attaches
    ``cache_control={"type": "ephemeral", "ttl": "5m"}`` to the final
    content block of the first system message. Idempotent: a no-op if
    there is no system message, if the system message isn't a dict (system
    messages are dicts in agent contexts), or if the last block already
    carries ``cache_control``.
    """
    if not _is_cache_control_allowed(model):
        return messages

    # The constructed system messages carry a ``cache_control`` field that
    # OpenAI's TypedDicts (the backbone of LitellmAnyMessage) don't model,
    # even though LiteLLM passes it through to Anthropic. Suppress the
    # return-type complaint at the construction sites only.
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content:
            block = {"type": "text", "text": content, "cache_control": _EPHEMERAL_CACHE}
            new_msg: LitellmAnyMessage = {"role": "system", "content": [block]}  # pyright: ignore[reportAssignmentType]
            return [*messages[:i], new_msg, *messages[i + 1 :]]
        if isinstance(content, list) and content:
            last = content[-1]
            if (
                isinstance(last, dict)
                and "cache_control" not in last
                and not _is_empty_text_block(last)
            ):
                cached_last = {**last, "cache_control": _EPHEMERAL_CACHE}
                new_msg: LitellmAnyMessage = {  # pyright: ignore[reportAssignmentType]
                    **msg,
                    "content": [*content[:-1], cached_last],
                }
                return [*messages[:i], new_msg, *messages[i + 1 :]]
        return messages
    return messages


def _with_cached_tools(
    tools: list[ChatCompletionToolParam], model: str
) -> list[ChatCompletionToolParam]:
    """Return ``tools`` with the last tool marked ephemerally cacheable.

    Anthropic's prompt cache extends from the start of the request through
    the *last* ``cache_control`` marker; placing one on the final tool
    definition caches the entire system+tools prefix together. With Stirrup
    loads of 7+ MCP tools and Opus tool schemas of a few KB each, this is
    typically the largest still-uncached chunk of the per-turn input.

    The marker sits at the top level of the tool dict (alongside ``"type"``
    and ``"function"``), where LiteLLM's Anthropic transformer picks it up
    when converting OpenAI-format tools into Anthropic's native shape.

    No-op unless the provider/model is allowlisted, the list is empty, or
    the last tool already carries ``cache_control``. Idempotent.
    """
    if not _is_cache_control_allowed(model):
        return tools
    if not tools:
        return tools
    last = tools[-1]
    if not isinstance(last, dict) or last.get("cache_control") is not None:
        return tools
    cached_last = {**last, "cache_control": _EPHEMERAL_CACHE}
    return [*tools[:-1], cached_last]  # pyright: ignore[reportReturnType]


def _with_cached_last_message(
    messages: list[LitellmAnyMessage], model: str
) -> list[LitellmAnyMessage]:
    """Return ``messages`` with the most recent message marked ephemerally cacheable.

    Conversation history is append-only across an agent loop, so marking the
    *last* message extends the cached prefix through every prior message.
    On the next turn — when one or two more messages have been appended —
    Anthropic's cache lookup falls back to this marker as the longest
    matching prefix and processes only the new suffix at full price.

    Operates on the last message regardless of role (assistant tool_use,
    tool result, user follow-up). String content is wrapped into a single
    text block; list content has its final block annotated.

    Most tool-calling agents in this repo append tool-result turns as
    ``LitellmOutputMessage(role="tool", ...)`` — a pydantic
    ``litellm.types.utils.Message``, not a dict — so once a trajectory has
    made its first tool call, the last message is a ``Message`` instance
    for the rest of the run. (Exceptions exist: e.g. ``afm_agent`` calls
    ``msg.model_dump()`` on every message before storing it, so its history
    is dicts throughout and it was already hitting the dict branch below.)
    ``Message`` allows extra fields (``model_config = {"extra": "allow"}``)
    and every ``anthropic_messages_pt`` code path reads ``cache_control`` via
    ``.get()``, which ``Message`` supports identically to a dict, so
    attaching it via a non-mutating ``model_copy`` reaches the wire request
    exactly like the dict branch below. Skipping ``Message`` instances here
    — as this function used to — means the rolling breakpoint never fires
    past the first tool call for most agents, leaving only the fixed
    system+tools prefix cached. litellm's output ``Message.content`` is
    type-validated to ``str | None``, so there is no list-of-blocks case to
    handle for this branch.

    Idempotent: skips when the target block already carries
    ``cache_control``.
    """
    if not _is_cache_control_allowed(model):
        return messages
    if not messages:
        return messages
    last = messages[-1]

    if isinstance(last, Message):
        if _content_is_empty(last.content) or last.get("cache_control") is not None:
            return messages
        cached_message = last.model_copy(update={"cache_control": _EPHEMERAL_CACHE})
        return [*messages[:-1], cached_message]

    if not isinstance(last, dict):
        return messages

    content = last.get("content")

    if isinstance(content, str):
        if not content.strip():
            return messages
        block = {"type": "text", "text": content, "cache_control": _EPHEMERAL_CACHE}
        new_msg: LitellmAnyMessage = {**last, "content": [block]}  # pyright: ignore[reportAssignmentType]
        return [*messages[:-1], new_msg]

    if isinstance(content, list) and content:
        last_block = content[-1]
        if (
            isinstance(last_block, dict)
            and "cache_control" not in last_block
            and not _is_empty_text_block(last_block)
        ):
            cached_block = {**last_block, "cache_control": _EPHEMERAL_CACHE}
            new_msg_list: LitellmAnyMessage = {  # pyright: ignore[reportAssignmentType]
                **last,
                "content": [*content[:-1], cached_block],
            }
            return [*messages[:-1], new_msg_list]

    return messages


def mark_last_message_cacheable(
    messages: list[LitellmAnyMessage], model: str
) -> list[LitellmAnyMessage]:
    """Public entry point for callers that append a transient, never-persisted
    suffix (e.g. a per-turn "N steps remaining" status ping) after the real
    conversation history, before handing the combined list to
    ``generate_response``.

    ``generate_response`` always marks whatever is literally the last message
    in what it's given. If that's a transient suffix that's different on
    every call (as with `web_research_agent`'s execution-status message), the
    marker lands on content that never recurs, so the cache write is wasted
    and nothing before it is ever read back — the rolling breakpoint silently
    stops extending past that point for the rest of the trajectory, even
    though the real history underneath is stable and repeats every call.

    Call this on the real, persisted message list *before* appending the
    transient suffix, so the breakpoint lands on content that will actually
    be resent unchanged next turn. ``generate_response`` will then also mark
    the transient tail message itself — harmless (one extra, low-cost cache
    write on a short string, still within Anthropic's 4-breakpoint limit) —
    but the breakpoint that matters, on the last real message, is already in
    place by the time it runs.
    """
    return _with_cached_last_message(messages, model)


_ANTHROPIC_EMPTY_TEXT_PLACEHOLDER = "[empty]"


def normalize_assistant_tool_call_content(
    messages: list[LitellmAnyMessage],
) -> list[LitellmAnyMessage]:
    """Ensure assistant messages with tool_calls have non-whitespace content.

    Some model endpoints (e.g. alabaster/100) return HTTP 500 when an assistant
    message has null or empty string content alongside tool_calls. Replacing it
    with an explicit placeholder satisfies those APIs and Anthropic's
    non-whitespace text validation.
    """
    normalized = []
    for msg in messages:
        if isinstance(msg, Message):
            if msg.role == "assistant" and msg.tool_calls and not msg.content:
                msg = msg.model_copy(
                    update={"content": _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER}
                )
        elif (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            and msg.get("tool_calls")
            and not msg.get("content")
        ):
            msg = {**msg, "content": _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER}
        normalized.append(msg)
    return normalized


def _with_nonempty_text_content(
    messages: list[LitellmAnyMessage],
) -> list[LitellmAnyMessage]:
    """Replace empty text content with a non-whitespace placeholder.

    Anthropic rejects any request containing an empty text block with
    ``messages: text content blocks must contain non-whitespace text`` (HTTP
    400). Empty *string* message content — an alternating-role placeholder
    user/assistant message, a tool result that produced no output, etc. —
    serializes to an empty text block and trips this. Filling with an explicit
    placeholder satisfies the API without leaving whitespace-only text that the
    provider now rejects. Empty list content and empty ``text`` blocks inside
    list content are handled the same way. ``None`` content is left untouched —
    it is omitted from the serialized request, which is valid (e.g. an
    assistant turn that is purely tool_calls).
    """
    placeholder = _ANTHROPIC_EMPTY_TEXT_PLACEHOLDER
    normalized: list[LitellmAnyMessage] = []
    for msg in messages:
        if isinstance(msg, Message):
            if msg.content == "":
                msg = msg.model_copy(update={"content": placeholder})
            normalized.append(msg)
            continue
        if isinstance(msg, dict):
            content = msg.get("content")
            if content == "" or content == []:
                msg = {**msg, "content": placeholder}
            elif isinstance(content, list):
                new_content = [
                    {**block, "text": placeholder}
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and not block.get("text")
                    else block
                    for block in content
                ]
                if new_content != content:
                    msg = {**msg, "content": new_content}  # pyright: ignore[reportArgumentType]
        normalized.append(msg)
    return normalized


def _drop_trailing_empty_assistant(
    messages: list[LitellmAnyMessage],
) -> list[LitellmAnyMessage]:
    """Drop a trailing assistant turn that carries no content.

    Task prompts are sometimes authored ending with an empty assistant
    "prefill" turn (``content == ""``). It is invalid downstream: filling it
    (see :func:`_with_nonempty_text_content`) lets the rolling cache-control
    breakpoint land on it, and Anthropic rejects the assistant *prefill*
    outright under extended thinking. Dropping the empty turn makes the
    conversation end on the user message, which is valid for every provider.

    Only a *truly empty* trailing assistant turn is dropped — turns carrying
    tool_calls, thinking_blocks, or reasoning_content are preserved (they hold
    state even when the text is empty), as are non-empty prefills.
    """
    if not messages:
        return messages
    last = messages[-1]
    if isinstance(last, Message):
        if (
            last.role == "assistant"
            and not last.tool_calls
            and not getattr(last, "thinking_blocks", None)
            and not getattr(last, "reasoning_content", None)
            and _content_is_empty(last.content)
        ):
            return messages[:-1]
        return messages
    if (
        isinstance(last, dict)
        and last.get("role") == "assistant"
        and not last.get("tool_calls")
        and not last.get("thinking_blocks")
        and not last.get("reasoning_content")
        and _content_is_empty(last.get("content"))
    ):
        return messages[:-1]
    return messages


# Stub result for a ``tool_call`` left unanswered at the tail of the history.
# Gemini wants a ``functionResponse`` for every ``functionCall`` it just emitted,
# so a bare user turn after a dangling call is a second invalid shape.
_ORPHAN_TOOL_RESULT_STUB = "(no result: the tool was not executed)"

# Appended after a trailing assistant turn that carries real content, so the
# request ends on a user turn without discarding what the model just said.
_GEMINI_CONTINUATION_NUDGE = "(Continue.)"


def _tool_call_id_and_name(tool_call: Any) -> tuple[str, str]:
    """``(id, name)`` for a tool_call in either dict or pydantic shape."""
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return tool_call.get("id") or "", function.get("name") or "unknown"
    function = getattr(tool_call, "function", None)
    call_id = getattr(tool_call, "id", "") or ""
    return call_id, getattr(function, "name", None) or "unknown"


def _effective_last_index(messages: list[LitellmAnyMessage]) -> int:
    """Index of the last message that survives litellm's Gemini transform.

    A ``user``/``system`` turn whose content yields no parts is dropped when the
    request is converted to ``contents``, so the message we hold last is not
    necessarily the turn Gemini sees last. An authored alternating-role
    placeholder (``content: []`` or a list of only empty text blocks -- the
    shape :func:`_with_nonempty_text_content` exists for) sitting after an
    assistant turn therefore still produces a trailing model turn.

    Measured against litellm's transform rather than assumed: only *list*
    content that yields no parts is dropped, and ``None`` with it. An empty or
    whitespace-only *string* still serializes to a text part and survives, so it
    is left alone -- skipping it would append a turn to a request that is
    already valid. ``tool`` turns survive even when empty (they become a
    ``functionResponse`` regardless).

    Returns ``-1`` when every message would be dropped.
    """
    idx = len(messages) - 1
    while idx >= 0:
        msg = messages[idx]
        content = get_msg_attr(msg, "content")
        dropped_by_transform = content is None or (
            isinstance(content, list) and _content_is_empty(content)
        )
        if get_msg_attr(msg, "role") in ("user", "system") and dropped_by_transform:
            idx -= 1
            continue
        return idx
    return -1


def _ensure_trailing_user_turn(
    messages: list[LitellmAnyMessage],
) -> list[LitellmAnyMessage]:
    """Return ``messages`` reshaped so the request does not end on a model turn.

    Gemini's ``generateContent`` rejects any request whose final ``contents``
    entry is ``role: "model"`` (``Requests ending with a model turn are not
    supported``, HTTP 400 INVALID_ARGUMENT). Nothing upstream guarantees the
    tail is a user turn: a task can be authored ending on an assistant prefill,
    an agent loop can append the model's reply and call again with no
    intervening tool result, and a role-flipped user-sim transcript can end on
    the persona's own last line. The 400 is deterministic, so it survives both
    the retry loop and a rerun of the trajectory.

    Three shapes, in order:

    * a *truly empty* trailing assistant turn is dropped (see
      :func:`_drop_trailing_empty_assistant`) -- an authored empty prefill
      carries nothing worth keeping, and the request then ends on the user turn;
    * a trailing assistant turn with unanswered ``tool_calls`` gets one stub
      tool result per call, so the request ends on a ``functionResponse``
      rather than a dangling ``functionCall``;
    * otherwise a minimal user turn is appended, preserving what the model
      actually said instead of discarding it.

    Non-mutating and idempotent -- a second pass sees a user/tool tail and
    returns the list untouched. The appended turn is never persisted:
    ``generate_response`` reshapes its own copy, so the caller's history, and
    what graders read back, is unchanged.
    """
    if not messages:
        return messages
    # Drop first, then classify: dropping an empty prefill can uncover another
    # assistant turn underneath, which still needs the nudge below.
    trimmed = _drop_trailing_empty_assistant(messages)
    if not trimmed:
        # The whole history was one empty assistant turn. Sending nothing is
        # not an improvement, so keep it and let the branches below shape it.
        trimmed = messages
    idx = _effective_last_index(trimmed)
    if idx < 0:
        return trimmed
    last = trimmed[idx]
    if get_msg_attr(last, "role") != "assistant":
        return trimmed
    tool_calls = get_msg_attr(last, "tool_calls")
    if tool_calls:
        stubs: list[LitellmAnyMessage] = []
        for tool_call in tool_calls:
            call_id, name = _tool_call_id_and_name(tool_call)
            # call_id/name are plain `str`, so the literal infers as
            # dict[str, str] rather than narrowing to ChatCompletionToolMessage.
            stub: LitellmAnyMessage = {  # pyright: ignore[reportAssignmentType]
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _ORPHAN_TOOL_RESULT_STUB,
            }
            stubs.append(stub)
        return [*trimmed, *stubs]
    nudge: LitellmAnyMessage = {
        "role": "user",
        "content": _GEMINI_CONTINUATION_NUDGE,
    }
    return [*trimmed, nudge]


def _is_context_window_error(e: Exception) -> bool:
    """
    Detect context window exceeded errors that LiteLLM doesn't properly classify.

    Some providers (notably Gemini) return context window errors as BadRequestError
    instead of ContextWindowExceededError. This predicate catches those cases
    by checking the error message content.

    Known error patterns:
    - Gemini: "input token count exceeds the maximum number of tokens allowed"
    - OpenAI: "context_length_exceeded" (usually caught as ContextWindowExceededError)
    - Anthropic: "prompt is too long" (usually caught as ContextWindowExceededError)
    """
    error_str = str(e).lower()

    # Common patterns indicating context/token limit exceeded
    context_patterns = [
        "token count exceeds",
        "context_length_exceeded",
        "context length exceeded",
        "maximum context length",
        "maximum number of tokens",
        "prompt is too long",
        "input too long",
        "exceeds the model's maximum context",
        "exceeds the context window",
    ]

    return any(pattern in error_str for pattern in context_patterns)


def _is_non_retriable_bad_request(e: Exception) -> bool:
    """
    Detect BadRequestErrors that are deterministic and should NOT be retried.

    These are configuration/validation errors that will always fail regardless
    of retry attempts. Retrying wastes time and resources.

    Note: Patterns must be specific enough to avoid matching transient errors
    like rate limits (e.g., "maximum of 100 requests" should NOT match).
    """
    error_str = str(e).lower()

    non_retriable_patterns = [
        # Tool count errors - be specific to avoid matching rate limits
        "tools are supported",  # "Maximum of 128 tools are supported"
        "too many tools",
        # Model/auth errors
        "model not found",
        "does not exist",
        "invalid api key",
        "authentication failed",
        "unauthorized",
        "unsupported parameter",
        "unsupported value",
        # OpenAI emits "Unknown parameter: 'foo'" for fields the model
        # endpoint doesn't accept (e.g. Responses-API `reasoning` sent to a
        # Chat-Completions-only model). These are config errors, not
        # transient — retrying just burns the worker for ~12 minutes.
        "unknown parameter",
        # Model capability mismatch
        "does not support multimodal",
        "is not a multimodal model",
        # Anthropic request validation (deterministic; retrying won't help)
        "text content blocks must be non-empty",
        "text content blocks must contain non-whitespace text",
        "max allowed size for many-image",
        "2000 pixels",
        "exceeds 5 mb",
        "5242880",
        "file format is invalid or unsupported",
        "image dimensions exceed max allowed size",
        "at least one non-system message",
        # OpenAI-family image payload rejections (deterministic): a single image
        # exceeds the decode-size limit, or the request carries too many images
        # ("request contains N images, exceeding the maximum of 50 allowed").
        # The "images," prefix keeps this off transient "maximum of N requests".
        "image decode limit exceeded",
        "images, exceeding the maximum of",
        # Vertex refused to fetch an image_url past its server-side cap. The same
        # url is refused on every attempt, so the retry loop only burned ~44
        # minutes of a Modal slot per failure. RLS-10563.
        "max_bytes_fetched",
        # Gemini rejects a request whose last turn is the model's own. The
        # identical payload is refused on every attempt. Deliberately NOT
        # mirrored into error.is_system_error: reaching the provider with this
        # shape is a harness bug, so the trajectory must stay a system ERROR
        # rather than being scored as a model failure.
        "ending with a model turn",
        # A provider whose endpoint only accepts inline base64 images rejected an
        # image_url with a remote URL (e.g. Tinker's tml/Inkling: "Image URL
        # input is not supported; pass images as base64 data URLs."). Deterministic
        # — the proxy image_url_to_base64_patch normally inlines these first, so
        # this only fires when that conversion could not run; retrying won't help.
        "pass images as base64",
        "image url input is not supported",
        # Content-moderation / policy refusals — the provider's safety filter
        # rejected the prompt (or generated content); retrying the identical
        # request is refused every time.
        "content management policy",
        "content policy",
        "content_filter",
        "the response was filtered",
        "contentpolicyviolation",
        "responsible ai",
        # Provider rejects an oversized asset/attachment (deterministic)
        # e.g. "Asset is too large: 2298798 bytes, max allowed is 2097152 bytes"
        "asset is too large",
        # A tool_call whose `arguments` string was truncated mid-value is stored
        # verbatim in the message history, so every later request replays those
        # bytes and a strictly-parsing provider rejects the whole body. Retrying
        # re-sends the identical fragment, so it can never clear: one batch burned
        # all 10 attempts and ~15.7h of backoff per trajectory before failing
        # anyway. Seen from Anthropic as "Failed to parse tool call arguments" and
        # via OpenRouter->Tencent as "forward bad request, [HTTP 400] ...
        # Unterminated string starting at: line 1 column 9 (char 8)".
        "unterminated string starting at",
        "failed to parse tool call arguments",
        # Lane can't accept the prompt's file block; the attachment replays on
        # every retry so it never clears. Burned 21 branches / 62 min once.
        "does not support file content blocks",
    ]

    return any(pattern in error_str for pattern in non_retriable_patterns)


def _should_skip_retry(e: Exception) -> bool:
    """Combined check for all non-retriable errors."""
    return (
        isinstance(e, budget_meter.BudgetExceededError)  # terminal: don't retry a stop
        or _is_context_window_error(e)
        or _is_non_retriable_bad_request(e)
    )


# Conservative default per-token rates, used only when litellm can't price the model.
# Without this the estimate would be $0 and the guardrail would silently stop
# enforcing; a premium-tier default over-estimates cheap models — the safe direction
# for a spend cap. (Accurate per-model rates via the orchestrator rate card are a
# follow-up.)
# One-shot guard so a disabled meter is reported once per process, not per call.
_meter_unavailable_logged = False


def _warn_once_if_meter_unavailable(budget_unit: str) -> None:
    """Say so when a metered trajectory has no Redis client.

    ``read_state`` returns None for a null client, so the gate silently admits every
    call: `runner/utils/redis.py` leaves ``redis_client`` as None unless all four of
    REDIS_HOST/PORT/USER/PASSWORD are set, and the import here swallows
    ImportError/ValueError. Failing open is deliberate for tests and offline runs, but
    in a metered prod container it means the guardrail does not exist for this
    trajectory and nothing anywhere says so. Behaviour is unchanged — this only makes
    it visible.
    """
    global _meter_unavailable_logged
    if _budget_redis is not None or _meter_unavailable_logged:
        return
    _meter_unavailable_logged = True
    logger.bind(event="budget_meter_unavailable", cost_unit=budget_unit).warning(
        "Spend guardrail inert: no Redis client in this runner process, so budget "
        f"gating is disabled for metered work (cost_unit={budget_unit})"
    )


_DEFAULT_INPUT_RATE = 1e-5
_DEFAULT_OUTPUT_RATE = 3e-5


def _model_rates(
    model: str, rate_overrides: dict[str, float] | None = None
) -> tuple[float, float, int]:
    """(input_$/tok, output_$/tok, default_max_output_tokens) for `model`.

    The server-threaded rate card (model_rates_ctx) wins — exact even when this
    image's litellm doesn't know the model; the container's litellm table is the
    fallback; a conservative non-zero default is last so the guardrail keeps
    enforcing. `rate_overrides` (caller-supplied, e.g. a per-run config value)
    wins over all of the above when explicitly provided.
    """
    in_rate = out_rate = 0.0
    max_out = 4096
    try:
        info = get_model_info(model)
        in_rate = info.get("input_cost_per_token") or 0.0
        out_rate = info.get("output_cost_per_token") or 0.0
        max_out = info.get("max_output_tokens") or 4096
    except Exception:
        pass
    ctx = model_rates_ctx.get()
    if ctx and ctx.get("input_cost_per_token"):
        in_rate = float(ctx["input_cost_per_token"])
        out_rate = float(ctx.get("output_cost_per_token") or 0.0)
    elif in_rate <= 0 and out_rate <= 0:
        logger.debug(f"budget: no rates for {model}; using default rates")
        in_rate, out_rate = _DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE
    overrides = rate_overrides or {}
    if overrides.get("input_cost_per_token") is not None:
        in_rate = float(overrides["input_cost_per_token"])
    if overrides.get("output_cost_per_token") is not None:
        out_rate = float(overrides["output_cost_per_token"])
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
    model: str, messages: list[Any], kwargs: dict[str, Any]
) -> float:
    """Pessimistic worst-case cost for one call: input tokens + max output, priced.

    Always non-zero when rates apply — never silently 0 (that would disable the cap).
    """
    in_rate, out_rate, default_max = _model_rates(model)
    try:
        in_tokens = token_counter(model=model, messages=messages)
    except Exception:
        in_tokens = max(1, len(str(messages)) // 4)  # ~4 chars/token fallback
    # Different call shapes cap output under different keys: max_tokens (chat),
    # max_completion_tokens (newer chat), max_output_tokens (Responses API).
    # Honor all three so the reservation isn't under-sized (BugBot).
    max_out = (
        kwargs.get("max_tokens")
        or kwargs.get("max_completion_tokens")
        or kwargs.get("max_output_tokens")
        or default_max
    )
    return in_tokens * in_rate + max_out * out_rate


def _resolve_rate(
    override: float | None, ctx_value: float | None, heuristic: float
) -> float:
    """First explicitly-set of (override, ctx_value), else heuristic.

    Unlike an `or` chain, an explicit ``0.0`` counts as set — a deliberately
    zeroed rate (e.g. a free/promo tier) must not fall through to the next
    tier just because it's falsy.
    """
    if override is not None:
        return float(override)
    if ctx_value is not None:
        return float(ctx_value)
    return heuristic


def compute_call_cost_usd(
    model: str,
    response: ModelResponse | None,
    rate_overrides: dict[str, float] | None = None,
) -> float | None:
    """Real settled cost from the response's usage, cache-aware. Handles both the
    chat-completions shape (prompt/completion_tokens) and the Responses-API shape
    (input/output_tokens). Returns ``None`` when usage is missing/unreadable, so
    the caller can settle at the reserved worst-case instead of $0 — real spend
    must never be counted as free (BugBot). `rate_overrides` (caller-supplied)
    wins over the model_rates_ctx/heuristic cache rates below, same as it wins
    inside `_model_rates` for input/output."""
    if response is None:
        return None
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        in_rate, out_rate, _ = _model_rates(model, rate_overrides)
        overrides = rate_overrides or {}
        ctx = model_rates_ctx.get() or {}
        cached_rate = _resolve_rate(
            overrides.get("cached_input_cost_per_token"),
            ctx.get("cached_input_cost_per_token"),
            in_rate * 0.1,
        )
        creation_rate = _resolve_rate(
            overrides.get("cache_creation_cost_per_token"),
            ctx.get("cache_creation_cost_per_token"),
            in_rate * 1.25,
        )
        # chat shape first, else Responses-API shape.
        prompt = int(
            getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
        )
        completion = int(
            getattr(usage, "completion_tokens", 0)
            or getattr(usage, "output_tokens", 0)
            or 0
        )
        if prompt == 0 and completion == 0:
            return None  # usage present but shape not understood → fall back
        details = getattr(usage, "prompt_tokens_details", None) or getattr(
            usage, "input_tokens_details", None
        )
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


async def _budget_gate(
    model: str, messages: Any, kwargs: dict[str, Any]
) -> tuple[str | None, str, float]:
    """Batch-spend guardrail gate: block the call when the batch is over budget,
    returning ``(budget_unit, budget_call_id, budget_est)`` for the post-call accrual.
    Inert unless enabled; fail-open on Redis trouble. On a deny, stashes the
    structured stop and raises ``BudgetExceededError``.
    """
    budget_unit = trajectory_batch_id_ctx.get() if budget_enabled_ctx.get() else None
    budget_call_id = f"{llm_call_id_ctx.get()}:{llm_attempt_ctx.get()}"
    budget_est = 0.0
    if budget_unit is not None:
        budget_est = _estimate_call_cost_usd(model, messages, kwargs)
        _warn_once_if_meter_unavailable(budget_unit)
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
            ).warning(f"LLM call denied: batch {budget_unit} over budget")
            raise budget_meter.BudgetExceededError(
                budget_unit, remaining_usd=snap.remaining_usd, model=model
            )
    return budget_unit, budget_call_id, budget_est


async def _budget_accrue(
    budget_unit: str | None,
    budget_call_id: str,
    budget_est: float,
    model: str,
    response_obj: Any,
    ok: bool,
) -> None:
    """Accrue the call's real cost into the batch's running total. On a successful
    call with unreadable usage, count $0 real cost (consistent with usage_metrics)
    and divert the estimate to the untracked side ledger (DD-alertable).
    """
    if budget_unit is None:
        return
    _cost = compute_call_cost_usd(model, response_obj) if ok else 0.0
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
        budget_meter.LANE_TRAJECTORY,
        untracked_est_usd=untracked_est,
    )


def _build_cas_spend_headers(
    *,
    workload: str,
    trajectory_id: str | None,
    existing: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build CAS spend-attribution tags from ambient ContextVars.

    Used both as LiteLLM ``extra_headers`` (when a proxy/gateway ``api_base`` is
    set) and as the ledger tag source for ``record_cas_success``. Attribution
    must not depend on routing config — without a proxy we still know the
    campaign / trajectory from ContextVars (see ``cas_direct_emit._spend_tags``).
    """
    hdrs = dict(existing or {})
    hdrs.pop("triggered_by", None)
    hdrs.setdefault("service", "trajectory")
    hdrs.setdefault("workload", workload)
    if work_unit := work_unit_ctx.get():
        hdrs.setdefault("work_unit", work_unit)
    if camp := campaign_id_ctx.get():
        hdrs.setdefault("campaign_id", camp)
    if acct := account_id_ctx.get():
        hdrs.setdefault("account_id", acct)
    if actor_uid := actor_user_id_ctx.get():
        hdrs.setdefault("user_id", actor_uid)
    resolved_trajectory_id = trajectory_id or trajectory_id_ctx.get()
    if resolved_trajectory_id:
        hdrs.setdefault("trajectory_id", resolved_trajectory_id)
    if world_id := world_id_ctx.get():
        hdrs.setdefault("world_id", world_id)
    if task_id := task_id_ctx.get():
        hdrs.setdefault("task_id", task_id)
    if batch_id := trajectory_batch_id_ctx.get():
        hdrs.setdefault("batch_id", batch_id)
    if triggered_by := triggered_by_ctx.get():
        hdrs["triggered_by"] = triggered_by
    # call_id stable across with_retry attempts; attempt is 1-indexed
    # so DD can compute unique_count(@call_id) vs count(*) for 429s.
    call_id = llm_call_id_ctx.get()
    if call_id:
        hdrs.setdefault("call_id", call_id)
        hdrs.setdefault("attempt", str(llm_attempt_ctx.get()))
    return hdrs


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
    skip_if=_should_skip_retry,
)
async def generate_response(
    model: str,
    messages: list[LitellmAnyMessage],
    tools: list[ChatCompletionToolParam],
    llm_response_timeout: int,
    extra_args: dict[str, Any],
    trajectory_id: str | None = None,
    stream: bool = False,
    mark_last_message: bool = True,
) -> ModelResponse:
    """
    Generate a response from the LLM with retry logic.

    Args:
        model: The model identifier to use
        messages: The conversation messages (input AllMessageValues or output Message)
        tools: Available tools for the model to call
        llm_response_timeout: Timeout in seconds for the LLM response
        extra_args: Additional arguments to pass to the completion call
        trajectory_id: Optional trajectory ID for tracking/tagging
        mark_last_message: Whether to mark the last message as the rolling
            cache breakpoint (see `_with_cached_last_message`). Set False when
            the caller already marked the real last message itself before
            appending a transient, never-persisted tail (e.g. a per-turn
            status ping) — otherwise this would mark that tail instead,
            wasting one of Anthropic's 4 available cache breakpoints on
            content that's different every call and so is never read back.
            See `mark_last_message_cacheable`.

    Returns:
        The model response
    """
    # Alabaster is a single endpoint with no fallback — cap at 3 retries to
    # avoid amplifying outages (RLS-10048). 0 = no override (use decorator default).
    llm_max_retries_ctx.set(3 if model.startswith("alabaster/") else 0)
    top_level_extra, extra_body = _split_extra_args(
        responses_args_to_completions(extra_args)
    )
    # Fireworks' LiteLLM provider rejects ``tool_choice`` outright (see
    # _is_fireworks_model). Forcing a tool is instead achieved by restricting
    # the ``tools`` list (e.g. tools=[final_answer]) — the harness-level control
    # — so dropping ``tool_choice`` here is behavior-neutral and keeps us immune
    # to whichever proxy lane/alias/wildcard the request routes through.
    if _is_fireworks_model(model):
        top_level_extra.pop("tool_choice", None)
        extra_body.pop("tool_choice", None)
    if model.startswith("anthropic/"):
        messages = apply_anthropic_image_policy(messages, tools, model=model)
        # A trailing empty assistant "prefill" turn (authored content == "")
        # would otherwise be placeholder-filled below and used as an invalid
        # assistant prefill under extended thinking. Drop it so the request
        # ends on the user turn.
        messages = _drop_trailing_empty_assistant(messages)
        # Anthropic 400s on any empty/whitespace-only text block; fill empty
        # content with a non-whitespace placeholder so e.g. an empty
        # alternating-role placeholder message does not fail the whole run.
        messages = _with_nonempty_text_content(messages)
    else:
        # Non-Anthropic endpoints with a per-asset size cap (e.g. tml/Inkling)
        # 400 on oversized inline images regardless of how they entered —
        # tool result, task-embedded image_url, or CLI-resolved. Shrink them to
        # fit at the send chokepoint (no-op for providers with no known cap).
        messages = apply_non_anthropic_image_policy(messages, model=model)
        # Vertex fetches an `image_url` server-side and 400s past 15 MiB, which
        # the policy above cannot see because it only rewrites inline data URIs.
        # Inline a re-encoded copy of an over-cap remote image (RLS-10563).
        messages = await apply_remote_image_fetch_policy(messages, model=model)
        # Gemini 400s on any request whose last turn is the model's own.
        # Nothing upstream guarantees the tail is a user/tool turn, so normalize
        # it here rather than in each agent.
        if _is_gemini_model(model):
            messages = _ensure_trailing_user_turn(messages)
    if top_level_extra.pop("object_tool_call_arguments", False):
        _normalize_tool_call_arguments(messages)
    cached_messages = _with_cached_system_prompt(messages, model)
    if mark_last_message:
        cached_messages = _with_cached_last_message(cached_messages, model)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": cached_messages,
        "timeout": llm_response_timeout,
        # Pin SDK-level retries to 0 — outer @with_retry owns all retry
        # logic for archipelago. Proxy is also pass-through (num_retries: 0).
        "num_retries": 0,
        **top_level_extra,
    }

    if tools:
        kwargs["tools"] = _with_cached_tools(tools, model)

    # If LiteLLM proxy is configured, route completions through it and add tags.
    # Mirrors call_responses_api: rely on explicit api_base/api_key — some SDK /
    # model paths ignore litellm.use_litellm_proxy for acompletion(), which would
    # send requests direct-to-provider (no proxy logging / spend attribution).
    #
    # Tags are sent two ways for redundancy:
    #   1. `metadata.tags` — the documented path. Lands in StandardLoggingPayload
    #      iff the proxy key has admin metadata `allow_client_tags: true`.
    #      In litellm 1.83.10+ a default-deny strip wipes this otherwise
    #      (BerriAI/litellm@0e62add).
    #   2. `extra_headers` — harvested by the proxy via
    #      `litellm_settings.extra_spend_tag_headers` in config.yaml. Bypasses
    #      the strip because header tags are appended in `_get_request_tags`
    #      AFTER the strip runs (litellm_logging.py:5249-5272). This is what
    #      actually makes tags flow today.
    # Docs: https://docs.litellm.ai/docs/proxy/cost_tracking
    # Route via the LLM Gateway (DEV only) or LiteLLM proxy + ship
    # spend-attribution tags through extra_headers. Tags via headers bypass
    # litellm 1.83.10's default-deny strip via the proxy's
    # `extra_spend_tag_headers` allowlist; gateway PR #32 forwards them.
    workload = (
        "trajectory_batch" if trajectory_batch_id_ctx.get() else "trajectory_single"
    )
    _log_llm_route_once(workload=workload)
    settings.apply_llm_target(
        kwargs,
        campaign_id=campaign_id_ctx.get(),
        workload=workload,
    )
    # Always resolve tags from ContextVars for CAS ledger emit. Attach the same
    # dict as LiteLLM spend-tag headers only when a proxy/gateway api_base is
    # set — attribution must not depend on routing config.
    spend_headers = _build_cas_spend_headers(
        workload=workload,
        trajectory_id=trajectory_id,
        existing=kwargs.get("extra_headers")
        if isinstance(kwargs.get("extra_headers"), dict)
        else None,
    )
    if kwargs.get("api_base"):
        kwargs["extra_headers"] = spend_headers

    _merge_extra_body(kwargs, extra_body)

    # --- batch-spend guardrail: reserve this call's worst-case cost, then reconcile
    # to actual in `finally`. ---
    budget_unit, budget_call_id, budget_est = await _budget_gate(
        model, cached_messages, kwargs
    )

    response_obj: ModelResponse | None = None
    start = time.perf_counter()
    ok = False
    error_type: str | None = None
    try:
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            stream_iter: Any = await acompletion(**kwargs)
            chunks: list[ModelResponse] = []

            async for chunk in stream_iter:
                chunks.append(chunk)
                # Per-delta progress logs only for external-agent-harness
                # providers (reasoning surfaced via `provider_specific_fields`).
                # These aren't model-internal chain-of-thought — they're
                # narrations the harness emits while it works (e.g. Gemini Deep
                # Research's thought summaries: "Origins and Architectural
                # Foundations — I am beginning by analyzing..."). Tag them as
                # `intermediate_output` so the UI can render them distinctly
                # from genuine `reasoning` logs from models like DeepSeek-R1
                # or the o-series. Native-reasoning streams go through
                # `delta.reasoning_content`, which stream_chunk_builder
                # aggregates and singleshot_agent logs once at end-of-call as
                # `reasoning` — no per-delta duplication from this path.
                intermediate_output = _extract_psf_reasoning_delta(chunk)
                if intermediate_output:
                    logger.bind(message_type="intermediate_output").info(
                        intermediate_output
                    )

            rebuilt = litellm.stream_chunk_builder(chunks, messages=messages)
            if rebuilt is None:
                raise RuntimeError("stream_chunk_builder returned None — empty stream")
            # ok flips only AFTER validation succeeds, so a parse/shape failure
            # is counted as status:error (not a false success on the baseline).
            validated = ModelResponse.model_validate(rebuilt)
            response_obj = validated
            ok = True
            return validated

        response = await acompletion(**kwargs)
        validated = ModelResponse.model_validate(response)
        response_obj = validated
        ok = True
        return validated
    except BaseException as exc:
        error_type = _classify_llm_error(exc)
        raise
    finally:
        await _budget_accrue(
            budget_unit, budget_call_id, budget_est, model, response_obj, ok
        )
        _emit_llm_latency_baseline(
            model=model,
            workload=workload,
            latency_seconds=time.perf_counter() - start,
            ok=ok,
            error_type=error_type,
        )
        # Best-effort Postgres ledger emit (complementary to spend-tag headers).
        # Soft-fails inside the emit helpers; must never affect the LLM result.
        # Both outcomes emit: @with_retry re-invokes this body per attempt and
        # the gateway meters each one, so a success-only ledger is short by
        # every failed attempt.
        cas_latency_ms = int((time.perf_counter() - start) * 1000)
        cas_call_id, cas_attempt = correlation_from_spend_headers(spend_headers)
        # `kwargs["api_key"]` is the shared proxy/gateway credential
        # `apply_llm_target` authenticated with; #19315 dropped it and
        # trajectory went 100% -> 0% because this file bypasses CasBridge and
        # so has no response-header fallback. See `shared_credential_fields`.
        #
        # TODO(INFR-2646): stamp `credential_kind` = "gateway_shared" here once
        # cas/registry/schemas/studio/v4.yaml has merged AND deployed to the
        # consumer. Until then do NOT bump CAS_TAG_SCHEMA_VERSION -- events
        # would arrive as unknown_schema_version and have their tags stripped.
        cas_credential = shared_credential_fields(kwargs.get("api_key"))
        if ok and response_obj is not None:
            record_cas_success(
                response_obj,
                model=model,
                tags=tags_from_spend_headers(spend_headers),
                backend="litellm",
                latency_ms=cas_latency_ms,
                **cas_credential,
            )
        elif not ok:
            record_cas_failure(
                model=model,
                tags=tags_from_spend_headers(spend_headers),
                backend="litellm",
                latency_ms=cas_latency_ms,
                error_code=error_type,
                call_id=cas_call_id,
                attempt=cas_attempt,
                **cas_credential,
            )


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
    skip_if=_should_skip_retry,
)
async def call_responses_api(
    model: str,
    messages: list[LitellmAnyMessage],
    tools: list[dict[str, Any]],
    llm_response_timeout: int,
    extra_args: dict[str, Any],
    trajectory_id: str | None = None,
    stream: bool = False,
) -> Any:
    """
    Generate a response using a provider's Responses API (e.g., web search) with retry logic.

    Uses litellm.aresponses() which is the native async version.

    Args:
        model: The model identifier to use (e.g., 'openai/gpt-4o')
        messages: The conversation messages
        tools: Tools for web search (e.g., [{"type": "web_search"}])
        llm_response_timeout: Timeout in seconds for the LLM response
        extra_args: Additional arguments (reasoning, etc.)
        trajectory_id: Optional trajectory ID for tracking/tagging

    Returns:
        The OpenAI responses API response object
    """
    llm_max_retries_ctx.set(3 if model.startswith("alabaster/") else 0)
    top_level_extra, extra_body = _split_extra_args(extra_args)
    # Studio-internal flag, never a provider param.
    top_level_extra.pop("object_tool_call_arguments", None)
    kwargs: dict[str, Any] = {
        "model": model,
        "input": messages,
        "tools": tools,
        "timeout": llm_response_timeout,
        # Pin SDK-level retries to 0 — see comment in generate_response.
        "num_retries": 0,
        **top_level_extra,
    }

    # Parity with generate_response above — see comments there.
    workload = (
        "trajectory_batch" if trajectory_batch_id_ctx.get() else "trajectory_single"
    )
    _log_llm_route_once(workload=workload)
    settings.apply_llm_target(
        kwargs,
        campaign_id=campaign_id_ctx.get(),
        workload=workload,
    )
    spend_headers = _build_cas_spend_headers(
        workload=workload,
        trajectory_id=trajectory_id,
        existing=kwargs.get("extra_headers")
        if isinstance(kwargs.get("extra_headers"), dict)
        else None,
    )
    if kwargs.get("api_base"):
        kwargs["extra_headers"] = spend_headers

    _merge_extra_body(kwargs, extra_body)

    # --- batch-spend guardrail: same reserve→reconcile as generate_response, so
    # Responses-API spend (web_research etc.) is metered too. ---
    budget_unit, budget_call_id, budget_est = await _budget_gate(
        model, messages, kwargs
    )

    response_obj: Any = None
    start = time.perf_counter()
    ok = False
    error_type: str | None = None
    try:
        if stream:
            kwargs["stream"] = True
            stream_iter: Any = await aresponses(**kwargs)
            completed_response = None
            async for event in stream_iter:
                if getattr(event, "type", None) == "response.completed":
                    completed_response = getattr(event, "response", None)
            if completed_response is None:
                raise RuntimeError(
                    "No response.completed event received from Responses API stream"
                )
            response_obj = completed_response
            ok = True
            return completed_response

        response = await aresponses(**kwargs)
        response_obj = response
        ok = True
        return response
    except BaseException as exc:
        error_type = _classify_llm_error(exc)
        raise
    finally:
        await _budget_accrue(
            budget_unit, budget_call_id, budget_est, model, response_obj, ok
        )
        _emit_llm_latency_baseline(
            model=model,
            workload=workload,
            latency_seconds=time.perf_counter() - start,
            ok=ok,
            error_type=error_type,
        )
        cas_latency_ms = int((time.perf_counter() - start) * 1000)
        cas_call_id, cas_attempt = correlation_from_spend_headers(spend_headers)
        # `kwargs["api_key"]` is the shared proxy/gateway credential
        # `apply_llm_target` authenticated with; #19315 dropped it and
        # trajectory went 100% -> 0% because this file bypasses CasBridge and
        # so has no response-header fallback. See `shared_credential_fields`.
        #
        # TODO(INFR-2646): stamp `credential_kind` = "gateway_shared" here once
        # cas/registry/schemas/studio/v4.yaml has merged AND deployed to the
        # consumer. Until then do NOT bump CAS_TAG_SCHEMA_VERSION -- events
        # would arrive as unknown_schema_version and have their tags stripped.
        cas_credential = shared_credential_fields(kwargs.get("api_key"))
        if ok and response_obj is not None:
            record_cas_success(
                response_obj,
                model=model,
                tags=tags_from_spend_headers(spend_headers),
                backend="litellm",
                latency_ms=cas_latency_ms,
                **cas_credential,
            )
        elif not ok:
            record_cas_failure(
                model=model,
                tags=tags_from_spend_headers(spend_headers),
                backend="litellm",
                latency_ms=cas_latency_ms,
                error_code=error_type,
                call_id=cas_call_id,
                attempt=cas_attempt,
                **cas_credential,
            )
