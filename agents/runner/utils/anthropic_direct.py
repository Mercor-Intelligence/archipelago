"""
Shared direct-Anthropic backend for the LiteLLM-bypass workaround agents.

Bypasses LiteLLM entirely — as of LiteLLM 1.83.x, recent Anthropic models
(Opus 4.7 / 4.8 and EAP codenames) are not in the chat-completion provider's
thinking-capable allowlist, so every shape that would enable extended thinking
(`thinking`, `reasoning_effort`, `output_config.effort: max/xhigh`) is either
rejected at the param allowlist or silently dropped at the Anthropic
translation layer. Here we call `anthropic.AsyncAnthropic().messages.create`
directly and translate between LiteLLM message/tool shapes (what the rest of
the agent stack uses) and Anthropic's native shapes.

The returned `ModelResponse` is a LiteLLM pydantic type, used purely as a data
carrier so an agent's `step()` can consume it unchanged. No LiteLLM API calls
are made.

`generate_response_anthropic` mirrors `runner.utils.llm.generate_response`'s
signature so an agent loop can swap backends cleanly. The model name is taken
verbatim (provider prefix stripped) — these agents source it from
`orchestrator_model`. The reasoning shape is supplied by the caller via
`extra_args`, which MUST be Anthropic Messages-API-native (the caller hardcodes
`ANTHROPIC_MAX_EFFORT_EXTRA_ARGS`, NOT LiteLLM chat-completion shapes).
"""

import json
import uuid
from typing import Any

from anthropic import APIConnectionError as AnthropicAPIConnectionError
from anthropic import APITimeoutError as AnthropicAPITimeoutError
from anthropic import AsyncAnthropic
from anthropic import BadRequestError as AnthropicBadRequestError
from anthropic import InternalServerError as AnthropicInternalServerError
from anthropic import RateLimitError as AnthropicRateLimitError
from anthropic.types import Message as AnthropicMessage
from litellm.exceptions import ContextWindowExceededError
from litellm.files.main import ModelResponse
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Choices,
    Function,
    Message,
    Usage,
)
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from runner.agents.models import (
    LitellmAnyMessage,
    content_to_str,
    get_msg_attr,
    get_msg_content,
    get_msg_role,
)
from runner.utils.decorators import with_retry
from runner.utils.settings import get_settings

# Routing prefixes the rest of the stack carries for LiteLLM model resolution.
# The Anthropic SDK expects just the bare model id, so we strip whichever
# matches. `anthropic_eap/` is the dedicated EAP routing lane (see the
# `anthropic_eap` provider + `batch_anthropic_eap` Modal queue).
_ANTHROPIC_PREFIXES = ("anthropic/", "anthropic_eap/")

# Native Anthropic Messages-API params for adaptive-thinking + max-effort —
# the combination LiteLLM's chat-completion path refuses to pass through for
# these models. Spread verbatim into `messages.create`. NOT LiteLLM shapes.
ANTHROPIC_MAX_EFFORT_EXTRA_ARGS: dict[str, Any] = {
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "max"},
    "max_tokens": 128000,
}

# Module-level clients (keyed by auth profile) so we reuse the underlying httpx
# connection pool across requests. Lazily initialized so importing this module
# doesn't require credentials to be resolvable (e.g. during server-side registry
# inspection). Containers are single-use (one model per run), but keying by
# profile keeps EAP and default auth from clobbering each other.
_clients: dict[str, AsyncAnthropic] = {}


def is_anthropic_model(model: str) -> bool:
    """True if `model` is an Anthropic model this backend can serve.

    Accepts the LiteLLM routing prefixes (`anthropic/`, `anthropic_eap/`) and
    bare `claude-*` ids.
    """
    lowered = model.lower()
    return lowered.startswith(_ANTHROPIC_PREFIXES) or lowered.startswith("claude")


def ensure_anthropic_model(model: str) -> None:
    """Fail fast if `model` is not an Anthropic model — this backend only speaks
    Anthropic's Messages API, so a non-Anthropic orchestrator is a config error."""
    if not is_anthropic_model(model):
        raise ValueError(
            "Anthropic-direct workaround agents only support Anthropic models; "
            f"got '{model}'. Point the orchestrator at an anthropic/ or "
            "anthropic_eap/ Claude model."
        )


def _is_eap_model(model: str) -> bool:
    """EAP models carry the dedicated `anthropic_eap/` routing prefix."""
    return model.lower().startswith("anthropic_eap/")


def _get_client(model: str) -> AsyncAnthropic:
    """Build the Anthropic client for `model`, routing through LiteLLM's
    passthrough when available.

    Agent runtimes have `LITELLM_PROXY_API_KEY` but not `ANTHROPIC_API_KEY`
    (the raw Anthropic key lives on the LiteLLM proxy side). LiteLLM's
    `/anthropic/*` passthrough forwards the request body to Anthropic using
    the proxy's stored credential, without running through LiteLLM's
    chat-completion translation layer — which is what blocks `thinking` and
    `output_config.effort: "max"` for these models. So we still get
    adaptive-thinking + max-effort through, just with proxy-brokered auth.

    Some models are gated to a distinct Anthropic workspace/credential (EAP
    access, or a workspace with data retention enabled). The proxy passthrough
    can't apply a per-model key, so these call Anthropic directly with their own
    key. The key is required for such models — there is no fallback to the
    default credential.
      - `anthropic_eap/*`            -> ANTHROPIC_EAP_API_KEY
      - `anthropic/claude-fable-5`   -> ANTHROPIC_FABLE_5_API_KEY

    Falls back to direct Anthropic (env-based `ANTHROPIC_API_KEY`) when no
    proxy is configured, which is the local-dev path.
    """
    settings = get_settings()

    if _is_eap_model(model):
        if "eap" not in _clients:
            _clients["eap"] = AsyncAnthropic(api_key=settings.ANTHROPIC_EAP_API_KEY)
        return _clients["eap"]
    if _to_anthropic_model(model) == "claude-fable-5":
        if "fable5" not in _clients:
            _clients["fable5"] = AsyncAnthropic(
                api_key=settings.ANTHROPIC_FABLE_5_API_KEY
            )
        return _clients["fable5"]

    if "default" not in _clients:
        if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
            base_url = settings.LITELLM_PROXY_API_BASE.rstrip("/") + "/anthropic"
            _clients["default"] = AsyncAnthropic(
                base_url=base_url,
                api_key=settings.LITELLM_PROXY_API_KEY,
            )
        else:
            _clients["default"] = AsyncAnthropic()
    return _clients["default"]


def _to_anthropic_model(model: str) -> str:
    for prefix in _ANTHROPIC_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


def _convert_tools(tools: list[ChatCompletionToolParam]) -> list[dict[str, Any]]:
    """OpenAI tool schema → Anthropic tool schema."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize a content value to a list of Anthropic content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    blocks.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    blocks.append({"type": "text", "text": text})
            elif btype == "image_url":
                # OpenAI-style image block → Anthropic image block
                url = (block.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    # data:<media_type>;base64,<data>
                    header, _, data = url.partition(",")
                    media_type = (
                        header.split(";")[0].removeprefix("data:") or "image/png"
                    )
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        }
                    )
                else:
                    blocks.append(
                        {"type": "image", "source": {"type": "url", "url": url}}
                    )
            elif btype == "image":
                # Pass Anthropic-native image blocks through unchanged.
                blocks.append(block)
        return blocks
    return []


def _tool_use_blocks_from_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """LiteLLM tool_calls → Anthropic tool_use blocks."""
    blocks: list[dict[str, Any]] = []
    if not tool_calls:
        return blocks
    for tc in tool_calls:
        if hasattr(tc, "function"):
            tc_id = getattr(tc, "id", None) or f"toolu_{uuid.uuid4().hex[:24]}"
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            raw_args = getattr(fn, "arguments", "") if fn else ""
        elif isinstance(tc, dict):
            tc_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
            fn_dict = tc.get("function") or {}
            name = fn_dict.get("name", "")
            raw_args = fn_dict.get("arguments", "")
        else:
            continue
        try:
            parsed_args = (
                json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            )
        except (json.JSONDecodeError, TypeError):
            parsed_args = {}
        blocks.append(
            {"type": "tool_use", "id": tc_id, "name": name, "input": parsed_args}
        )
    return blocks


def _convert_messages(
    messages: list[LitellmAnyMessage],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """LiteLLM message list → (system_blocks, anthropic_messages).

    - System messages are pulled out (Anthropic takes `system` as a top-level param).
    - Tool-role messages are rolled into the next user message as `tool_result` blocks.
    - Assistant messages preserve thinking_blocks (with signatures) for extended thinking.
    """
    system_blocks: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def _flush_tool_results() -> None:
        if pending_tool_results:
            converted.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        role = get_msg_role(msg)
        content = get_msg_content(msg)

        if role == "system":
            text = content_to_str(content)
            if text:
                system_blocks.append({"type": "text", "text": text})
            continue

        if role == "tool":
            tool_use_id = get_msg_attr(msg, "tool_call_id", "") or ""
            if isinstance(content, list):
                tool_content = _text_blocks(content) or [{"type": "text", "text": ""}]
            else:
                tool_content = [{"type": "text", "text": content_to_str(content)}]
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": tool_content,
                }
            )
            continue

        # Any non-tool message flushes any accumulated tool_results into a user turn.
        _flush_tool_results()

        if role == "user":
            blocks = _text_blocks(content)
            if blocks:
                converted.append({"role": "user", "content": blocks})
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            # Preserve thinking blocks (with signatures) — required for extended
            # thinking on any assistant turn that includes tool_use blocks.
            thinking_blocks = get_msg_attr(msg, "thinking_blocks", None)
            if isinstance(thinking_blocks, list):
                for tb in thinking_blocks:
                    if isinstance(tb, dict) and tb.get("type") in {
                        "thinking",
                        "redacted_thinking",
                    }:
                        blocks.append(tb)
            blocks.extend(_text_blocks(content))
            blocks.extend(
                _tool_use_blocks_from_tool_calls(get_msg_attr(msg, "tool_calls"))
            )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue

    _flush_tool_results()

    # Anthropic requires alternating user/assistant roles. Merge consecutive
    # same-role messages by concatenating their content blocks.
    merged: list[dict[str, Any]] = []
    for m in converted:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {
                "role": m["role"],
                "content": list(merged[-1]["content"]) + list(m["content"]),
            }
        else:
            merged.append(m)

    return system_blocks, merged


_STOP_REASON_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def _convert_response(response: AnthropicMessage) -> ModelResponse:
    """Anthropic Message → LiteLLM ModelResponse (pure data conversion)."""
    text_parts: list[str] = []
    thinking_blocks: list[dict[str, Any]] = []
    tool_calls: list[ChatCompletionMessageToolCall] = []

    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "thinking":
            thinking_blocks.append(
                {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", "") or "",
                    "signature": getattr(block, "signature", "") or "",
                }
            )
        elif btype == "redacted_thinking":
            thinking_blocks.append(
                {"type": "redacted_thinking", "data": getattr(block, "data", "") or ""}
            )
        elif btype == "tool_use":
            tool_calls.append(
                ChatCompletionMessageToolCall(
                    id=getattr(block, "id", "") or f"toolu_{uuid.uuid4().hex[:24]}",
                    function=Function(
                        name=getattr(block, "name", "") or "",
                        arguments=json.dumps(getattr(block, "input", {}) or {}),
                    ),
                    type="function",
                )
            )

    content_str = "".join(text_parts) if text_parts else None
    finish_reason = _STOP_REASON_MAP.get(response.stop_reason or "end_turn", "stop")

    message_kwargs: dict[str, Any] = {
        "role": "assistant",
        "content": content_str,
    }
    if tool_calls:
        message_kwargs["tool_calls"] = tool_calls
    if thinking_blocks:
        message_kwargs["thinking_blocks"] = thinking_blocks

    choice = Choices(
        finish_reason=finish_reason,
        index=0,
        message=Message(**message_kwargs),
    )

    usage_obj = response.usage
    prompt_tokens = getattr(usage_obj, "input_tokens", 0) or 0
    completion_tokens = getattr(usage_obj, "output_tokens", 0) or 0
    usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )

    return ModelResponse(
        id=response.id,
        model=response.model,
        choices=[choice],
        usage=usage,
    )


# Anthropic's own API can return prompt-too-long errors as BadRequestError;
# surface them as ContextWindowExceededError so an agent's existing handler
# triggers summarization instead of retrying.
_CONTEXT_PATTERNS = (
    "prompt is too long",
    "maximum context length",
    "exceeds the context window",
    "input is too long",
)


def _maybe_reraise_as_context_window(exc: AnthropicBadRequestError) -> None:
    msg = str(exc).lower()
    if any(p in msg for p in _CONTEXT_PATTERNS):
        raise ContextWindowExceededError(
            message=str(exc), model="anthropic", llm_provider="anthropic"
        ) from exc


@with_retry(
    max_retries=10,
    base_backoff=5,
    jitter=5,
    retry_on=(
        AnthropicRateLimitError,
        AnthropicAPITimeoutError,
        AnthropicAPIConnectionError,
        AnthropicInternalServerError,
    ),
    skip_on=(ContextWindowExceededError,),
)
async def generate_response_anthropic(
    model: str,
    messages: list[LitellmAnyMessage],
    tools: list[ChatCompletionToolParam],
    llm_response_timeout: int,
    extra_args: dict[str, Any],
    trajectory_id: str | None = None,  # noqa: ARG001 — parity with generate_response; Anthropic has no tag equivalent
) -> ModelResponse:
    """Call Anthropic directly and return a LiteLLM-shaped `ModelResponse`.

    Mirrors `runner.utils.llm.generate_response`'s signature so the agent's
    step() loop can swap backends cleanly. No LiteLLM calls are made.
    """
    system_blocks, anthropic_messages = _convert_messages(messages)
    anthropic_tools = _convert_tools(tools) if tools else []

    kwargs: dict[str, Any] = {
        "model": _to_anthropic_model(model),
        "messages": anthropic_messages,
        "timeout": llm_response_timeout,
        **extra_args,
    }
    if system_blocks:
        kwargs["system"] = system_blocks
    if anthropic_tools:
        kwargs["tools"] = anthropic_tools
    if "max_tokens" not in kwargs:
        # Anthropic's API requires max_tokens. Match the hardcoded default.
        kwargs["max_tokens"] = 128000

    client = _get_client(model)
    try:
        response = await client.messages.create(**kwargs)
    except AnthropicBadRequestError as e:
        _maybe_reraise_as_context_window(e)
        raise

    return _convert_response(response)
