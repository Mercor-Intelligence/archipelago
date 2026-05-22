"""LLM utilities for grading runner."""

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
    ContextWindowExceededError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.files.main import ModelResponse
from pydantic import BaseModel

import runner.utils.litellm_patches  # noqa: F401
from runner.utils.decorators import (
    llm_attempt_ctx,
    llm_call_id_ctx,
    with_concurrency_limit,
    with_retry,
)
from runner.utils.settings import get_settings

settings = get_settings()

# Configure LiteLLM proxy routing if configured
if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
    litellm.use_litellm_proxy = True

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

    The provider is read from the model prefix (``"anthropic"`` from
    ``"anthropic/claude-opus-4-7"``) rather than via
    ``litellm.get_llm_provider`` — that helper short-circuits to
    ``"litellm_proxy"`` whenever ``litellm.use_litellm_proxy`` is set
    (always true in Studio prod), losing the underlying provider info.
    """
    provider = model.split("/", 1)[0] if "/" in model else None
    return provider in _CACHE_CONTROL_ALLOWLIST or model in _CACHE_CONTROL_ALLOWLIST


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

    No-op unless the model's provider prefix or the full model string is
    in :class:`CacheControlAllowlist`. On a match, attaches
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
    if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
        kwargs.setdefault("api_base", settings.LITELLM_PROXY_API_BASE)
        kwargs.setdefault("api_key", settings.LITELLM_PROXY_API_KEY)
        grading_run_id = grading_run_id_ctx.get()
        # call_id is stable across `with_retry` attempts of the same logical
        # call; attempt is 1-indexed and increments per retry. Together they
        # let Datadog distinguish unique logical calls from retry attempts
        # (e.g. unique_count(@call_id) vs count(*) for the 429 rate).
        call_id = llm_call_id_ctx.get()
        attempt_num = llm_attempt_ctx.get() if call_id else None
        hdrs = dict(kwargs.get("extra_headers") or {})
        hdrs.setdefault("service", "grading")
        if grading_run_id:
            hdrs.setdefault("grading_run_id", grading_run_id)
        if call_id:
            hdrs.setdefault("call_id", call_id)
            hdrs.setdefault("attempt", str(attempt_num))
        kwargs["extra_headers"] = hdrs

    response = await litellm.acompletion(**kwargs)
    return ModelResponse.model_validate(response)
