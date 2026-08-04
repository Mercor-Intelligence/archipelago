"""Error classification logic for distinguishing system errors from model errors."""

import httpx
from litellm.exceptions import (
    APIConnectionError,
    BadRequestError,
    ContextWindowExceededError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from mcp import McpError

from runner.utils.image_fetch import (
    AnthropicRequestImageBudgetError,
    ImageFetchError,
)


def is_system_error(exception: Exception) -> bool:
    """Determine if an exception represents a system error (retryable) vs model error.

    System errors are transient infrastructure issues that can be retried.
    Model errors are non-retryable failures like context overflow.

    Returns:
        True if the exception is a system error (should use ERROR status),
        False if it's a model error (should use FAILED status).
    """
    if isinstance(
        exception,
        (
            RateLimitError,
            Timeout,
            ServiceUnavailableError,
            APIConnectionError,
            InternalServerError,
        ),
    ):
        return True

    # BadRequestError could be either, check the error message
    if isinstance(exception, BadRequestError):
        error_str = str(exception).lower()
        if "exceeded your current quota" in error_str:
            return True  # System error
        # If it's context/token/multimodal related, it's a model error
        if (
            "context" in error_str
            or "token" in error_str
            or "is not a multimodal model" in error_str
            or "does not support multimodal" in error_str
        ):
            return False  # Model error
        # Anthropic request validation (deterministic)
        if (
            "text content blocks must be non-empty" in error_str
            or "text content blocks must contain non-whitespace text" in error_str
            or "max allowed size for many-image" in error_str
            or "2000 pixels" in error_str
            or "exceeds 5 mb" in error_str
            or "5242880" in error_str
            # Provider rejects an oversized asset/attachment (deterministic;
            # e.g. "Asset is too large: 2298798 bytes, max allowed is 2097152")
            or "asset is too large" in error_str
        ):
            return False  # Model error
        # Content-moderation / policy refusals. The provider's safety filter
        # rejected the prompt (or generated content); the identical request is
        # refused on every retry (litellm ContentPolicyViolationError is a
        # BadRequestError subclass, and also arrives wrapped as a 400 carrying
        # these strings). A genuine model failure, not a retryable system error.
        if (
            "content management policy" in error_str
            or "content policy" in error_str
            or "content_filter" in error_str
            or "the response was filtered" in error_str
            or "contentpolicyviolation" in error_str
            or "responsible ai" in error_str
        ):
            return False  # Model error
        # OpenAI-family image payload rejections (deterministic): a single image
        # exceeds the decode-size limit, or the request carries too many images
        # ("request contains N images, exceeding the maximum of 50 allowed").
        # The "images," prefix keeps this off transient "maximum of N requests"
        # and matches the retry skip-list in llm._is_non_retriable_bad_request.
        if (
            "image decode limit exceeded" in error_str
            or "images, exceeding the maximum of" in error_str
        ):
            return False  # Model error
        return True  # System error (configuration/infrastructure issue)

    # Model errors (non-retryable)
    if isinstance(exception, ContextWindowExceededError):
        return False

    # ValueError is typically a configuration/validation error (non-retryable)
    if isinstance(exception, ValueError):
        return False

    # RuntimeError (e.g. SSE error events) — non-retryable
    if isinstance(exception, RuntimeError):
        return False

    # Image fetch failures are deterministic input/contract problems
    # (oversize, unsupported scheme, bad URL, 4xx). Retrying won't fix them.
    if isinstance(exception, ImageFetchError):
        return False

    if isinstance(exception, AnthropicRequestImageBudgetError):
        return False

    # httpx HTTP errors — 5xx are retryable, 4xx are not
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code >= 500

    # httpx transport/connection errors are retryable system errors
    if isinstance(exception, (httpx.ConnectError, httpx.ReadError, httpx.WriteError)):
        return True

    # Unknown exceptions default to system error (safer to retry than fail permanently)
    return True


# Substrings that mark an already-stringified provider/infra error as a system
# (retryable) error. Mirrors the exception classes in `is_system_error` for
# failures that arrive as text rather than as a raised exception — e.g. the
# Antigravity app surfaces a swallowed 429 in the antigravity_run response's
# `error` field instead of raising (the bundled SDK logs the 429 and ends the
# stream without propagating an exception).
_SYSTEM_ERROR_MESSAGE_MARKERS = (
    "exceeded your current quota",
    "resource_exhausted",
    "rate limit",
    "code 429",
    "(http 429)",
    "service unavailable",
    "internal server error",
    "model provider error",
    "terminated due to error",
    "retryable error from model provider",
    "connection error",
)


def is_system_error_message(message: str | None) -> bool:
    """String-based companion to `is_system_error` for provider/infra errors.

    Some failures reach us as text rather than as a raised exception — notably
    the Antigravity agent, whose bundled SDK swallows terminal provider errors
    (e.g. a 429) and surfaces them in the run response's `error` field. This
    classifies such a string as a system (retryable, ERROR-status) error.

    Deliberately conservative: unknown text returns False, so a genuine
    model/agent failure stays FAILED rather than being mislabeled a retryable
    system error.
    """
    if not message:
        return False
    low = message.lower()
    return any(marker in low for marker in _SYSTEM_ERROR_MESSAGE_MARKERS)


def is_fatal_mcp_error(exception: Exception) -> bool:
    """Determine if an exception is fatal and should immediately end the agent run.

    Fatal errors indicate the MCP session/connection is dead and cannot recover.
    Non-fatal errors can be reported to the LLM and the agent can continue.

    Args:
        exception: The exception to check.

    Returns:
        True if the error is fatal (session terminated, connection dead),
        False if the error is recoverable.
    """
    # Check for MCP-specific errors
    if isinstance(exception, McpError):
        # Check error code - handle both positive 32600 (current MCP bug) and
        # negative -32600 (JSON-RPC 2.0 standard) for forward compatibility
        error_code = (
            getattr(exception.error, "code", None)
            if hasattr(exception, "error")
            else None
        )
        if error_code in (32600, -32600):
            return True

        # Fallback to string matching for robustness
        if "Session terminated" in str(exception):
            return True

    # Check for FastMCP client disconnection errors
    if isinstance(exception, RuntimeError):
        error_str = str(exception)
        # FastMCP raises this when the client session has been closed/corrupted
        if "Client is not connected" in error_str:
            return True

    return False
