"""Thin wrapper over Prime Intellect's ``renderers`` library for flat TITO.

Isolates the heavy, lazily-imported ``renderers`` + ``transformers`` dependency
and the policy-model -> HF-tokenizer resolution behind the small surface the
capture session needs:

  * ``render_ids``  — first turn: messages -> prompt token ids
  * ``bridge``      — later turns: extend (prev_prompt, prev_completion) with the
                      new tool/user messages, byte-for-byte, or ``None`` if the
                      extension can't be proven safe (history rewrite, etc.)
  * ``parse``       — sampled ids -> structured content/reasoning/tool calls so
                      the harness loop keeps working unchanged

Only exact text-only models registered by the pinned ``renderers`` release are
accepted. Default and multimodal renderers cannot satisfy this transport's
byte-coherence contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# Provider/transport prefixes stripped to recover a bare model name.
_PROVIDER_PREFIXES = (
    "fireworks_ai/",
    "hosted_vllm/",
    "vllm/",
    "openai/",
)
_RESPONSES_SEGMENT = "responses"


@dataclass(frozen=True)
class TitoParsedToolCall:
    id: str | None
    name: str
    arguments: dict[str, Any] | str | None


@dataclass(frozen=True)
class TitoParsedResponse:
    content: str | None
    reasoning_content: str | None
    tool_calls: list[TitoParsedToolCall]
    parse_errors: list[str]


@lru_cache(maxsize=1)
def registered_text_model_ids() -> frozenset[str]:
    from renderers.base import MODEL_RENDERER_MAP, MULTIMODAL_MODELS  # noqa: PLC0415

    return frozenset(MODEL_RENDERER_MAP).difference(MULTIMODAL_MODELS)


def _without_responses_segment(model: str) -> str:
    return "/".join(part for part in model.split("/") if part != _RESPONSES_SEGMENT)


def _model_candidates(model: str) -> list[str]:
    name = _without_responses_segment(model)
    candidates = [name]
    changed = True
    while changed:
        changed = False
        for prefix in _PROVIDER_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                candidates.append(name)
                changed = True
                break
    return candidates


def resolve_hf_id(policy_model: str) -> str:
    """HF tokenizer repo id for the policy model.

    An explicit ``MERCOR_POLICY_HF_ID`` override wins, because provider ids
    (Fireworks account paths, vLLM served names) don't map 1:1 to HF repos.
    Otherwise resolve the first exact registered text-model candidate.
    """
    override = os.environ.get("MERCOR_POLICY_HF_ID", "").strip()
    if override:
        return override
    supported = registered_text_model_ids()
    for candidate in _model_candidates(policy_model):
        if candidate in supported:
            return candidate
    return _without_responses_segment(policy_model)


def validate_hf_id(hf_id: str) -> None:
    if hf_id not in registered_text_model_ids():
        raise RuntimeError(
            f"TITO requires an exact text-only model registered by renderers: {hf_id!r}"
        )


def _is_default_renderer(renderer: Any) -> bool:
    """True if ``renderers`` fell back to its generic, model-agnostic renderer."""
    return type(renderer).__name__ == "DefaultRenderer"


def _as_ids(rendered: Any) -> list[int]:
    """Coerce a renderers return value to a token-id list."""
    ids = getattr(rendered, "token_ids", rendered)
    return list(ids)


def _text_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict) or part.get("type") not in {
            "text",
            "input_text",
            "output_text",
        }:
            raise ValueError("TITO text-only capture does not support non-text content")
        text = part.get("text")
        if not isinstance(text, str):
            raise ValueError("TITO text-only content parts require string text")
        parts.append(text)
    return "".join(parts)


def normalize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("TITO renderer messages must serialize to dictionaries")
        item = dict(message)
        if "content" in item:
            item["content"] = _text_content(item["content"])
        normalized.append(item)
    return normalized


def _build_renderer(hf_id: str, chat_template_kwargs: dict[str, Any]) -> Any:
    """Lazily import the heavy deps and build an exact registered renderer."""
    from renderers import (  # noqa: PLC0415
        AutoRendererConfig,
        create_renderer,
        is_multimodal,
    )
    from renderers.base import load_tokenizer  # noqa: PLC0415

    validate_hf_id(hf_id)
    tokenizer = load_tokenizer(hf_id)
    renderer = create_renderer(
        tokenizer,
        AutoRendererConfig(thinking_retention="all"),
        chat_template_kwargs=chat_template_kwargs or None,
    )
    if _is_default_renderer(renderer) or is_multimodal(renderer):
        raise RuntimeError(f"TITO model is not text-only: {hf_id!r}")
    return renderer


class TitoRenderer:
    """Renderers wrapper for one policy model and template configuration."""

    def __init__(
        self, hf_id: str, chat_template_kwargs: dict[str, Any] | None = None
    ) -> None:
        self.hf_id = hf_id
        self.chat_template_kwargs = dict(chat_template_kwargs or {})
        self._renderer = _build_renderer(hf_id, self.chat_template_kwargs)

    def render_ids(self, messages: list[Any], tools: list[Any] | None) -> list[int]:
        rendered = self._renderer.render(
            normalize_messages(messages),
            tools=tools,
            add_generation_prompt=True,
        )
        if getattr(rendered, "multi_modal_data", None) is not None:
            raise ValueError("TITO text-only capture received multimodal renderer data")
        return _as_ids(rendered)

    def bridge(
        self,
        prev_prompt_ids: list[int],
        prev_completion_ids: list[int],
        new_messages: list[Any],
        tools: list[Any] | None,
    ) -> list[int] | None:
        """Extended next-turn prompt ids, or ``None`` if unsafe to extend."""
        out = self._renderer.bridge_to_next_turn(
            prev_prompt_ids,
            prev_completion_ids,
            normalize_messages(new_messages),
            tools=tools,
        )
        if out is None:
            return None
        if getattr(out, "multi_modal_data", None) is not None:
            raise ValueError("TITO text-only bridge received multimodal renderer data")
        return _as_ids(out)

    def parse(
        self, completion_ids: list[int], tools: list[Any] | None
    ) -> TitoParsedResponse:
        """Parse sampled ids while retaining only valid executable tool calls."""
        parsed = self._renderer.parse_response(completion_ids, tools=tools)
        tool_calls: list[TitoParsedToolCall] = []
        parse_errors: list[str] = []
        for call in getattr(parsed, "tool_calls", None) or []:
            status = getattr(getattr(call, "status", None), "value", None)
            name = getattr(call, "name", None)
            if status != "ok" or not isinstance(name, str) or not name:
                parse_errors.append(str(status or "missing_name"))
                continue
            tool_calls.append(
                TitoParsedToolCall(
                    id=getattr(call, "id", None),
                    name=name,
                    arguments=getattr(call, "arguments", None),
                )
            )
        content = getattr(parsed, "content", None)
        reasoning = getattr(parsed, "reasoning_content", None)
        return TitoParsedResponse(
            content=content if isinstance(content, str) else None,
            reasoning_content=reasoning if isinstance(reasoning, str) else None,
            tool_calls=tool_calls,
            parse_errors=parse_errors,
        )
