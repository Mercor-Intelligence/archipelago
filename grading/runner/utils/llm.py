"""LLM utilities for grading runner."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
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

from runner.utils.decorators import with_concurrency_limit, with_retry
from runner.utils.settings import get_settings

settings = get_settings()

# Configure LiteLLM proxy routing if configured
if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
    litellm.use_litellm_proxy = True

# Default concurrency limit for LLM calls
LLM_CONCURRENCY_LIMIT = 10

# Context variable for grading run ID
grading_run_id_ctx: ContextVar[str | None] = ContextVar("grading_run_id", default=None)


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
        "messages": messages,
        "timeout": timeout,
        **(extra_args or {}),
    }

    if response_format:
        kwargs["response_format"] = response_format

    # If LiteLLM proxy is configured, add tracking tags
    if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
        tags = ["service:grading"]
        grading_run_id = grading_run_id_ctx.get()
        if grading_run_id:
            tags.append(f"grading_run_id:{grading_run_id}")
        kwargs["extra_body"] = {"tags": tags}

    response = await litellm.acompletion(**kwargs)
    return ModelResponse.model_validate(response)
