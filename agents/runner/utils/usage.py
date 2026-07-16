"""Token usage tracking for agent LLM calls."""

import base64
import json
import math
import struct
from typing import Any

import litellm
from litellm.files.main import ModelResponse


def _coerce_int(value: Any) -> int:
    """Defensively coerce a usage attribute to a non-negative int.

    LiteLLM occasionally leaves details fields as None, strings, or floats
    depending on the upstream provider's shape, so we normalize here.
    """
    if value is None:
        return 0
    try:
        return int(value) if int(value) > 0 else 0
    except (TypeError, ValueError):
        return 0


def _get_attr_or_item(obj: Any, key: str) -> Any:
    """Read a key from either a pydantic model / dataclass or a plain dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


class UsageTracker:
    """Accumulates token usage across multiple LLM calls during agent execution."""

    def __init__(
        self,
        *,
        track_token_breakdown: bool = False,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.final_answer_tokens: int = 0
        self.max_prompt_tokens: int = 0
        self.compaction_count: int = 0
        # Provider-specific detail fields. LiteLLM normalizes these onto
        # `usage.completion_tokens_details` / `usage.prompt_tokens_details`,
        # so we extract them once here and let downstream consumers persist
        # them alongside the basic counts.
        self.reasoning_tokens: int = 0
        self.cached_tokens: int = 0
        self.cache_creation_tokens: int = 0
        # Per-call log: one entry per LLM call in order of execution.
        self.call_log: list[dict[str, int]] = []
        # Opt-in per-step breakdown (visible / tool-arg / tool-result /
        # reasoning-summary / marginal splits). Off by default so other
        # harnesses keep their existing call_log shape unchanged.
        self._breakdown: bool = track_token_breakdown
        self._model: str = model or "gpt-4o"
        # Set lazily from the first LLM response's custom_llm_provider. Used
        # provider-first by _image_tokens; "" falls back to model-name keywords.
        self._provider: str = (provider or "").lower()
        self._prev_prompt_tokens: int = 0
        # Counts compactions since the last tracked call; consumed by the next
        # track() to flag the post-compaction step (its prompt drops because the
        # context was just summarized, which would otherwise break the per-step
        # token identities). A count, not a bool, so two compactions before one
        # tracked response still reconcile with compaction_count.
        self._pending_compactions: int = 0

    def track_compaction(self) -> None:
        """Increment compaction count when a context summarization LLM call fires.

        Also arms a flag so the next tracked call (whose prompt drops because the
        context was just summarized) is marked compacted in the breakdown, letting
        downstream identity checks exclude that row.
        """
        self.compaction_count += 1
        self._pending_compactions += 1

    def _capture_provider(self, source: Any) -> None:
        """Record the litellm provider once, from a response's hidden params.

        litellm tags every response with ``_hidden_params.custom_llm_provider``
        (e.g. ``anthropic``, ``vertex_ai``, ``gemini``). That is a more reliable
        family signal than the model name (aliases like ``ajax`` don't carry it).
        Only set once and only if still unknown; fully defensive on shape.
        """
        if self._provider:
            return
        try:
            if isinstance(source, dict):
                hidden = source.get("_hidden_params") or {}
            else:
                hidden = getattr(source, "_hidden_params", None) or {}
            provider = (
                hidden.get("custom_llm_provider") if isinstance(hidden, dict) else None
            )
            if isinstance(provider, str) and provider:
                self._provider = provider.lower()
        except Exception:
            pass

    def track(self, response: ModelResponse) -> None:
        """Extract and accumulate usage from a ModelResponse."""
        self._capture_provider(response)
        usage = getattr(response, "usage", None)
        if usage is None:
            return

        call_prompt_tokens = _coerce_int(getattr(usage, "prompt_tokens", 0))
        call_completion_tokens = _coerce_int(getattr(usage, "completion_tokens", 0))

        self.prompt_tokens += call_prompt_tokens
        self.completion_tokens += call_completion_tokens
        self.final_answer_tokens = call_completion_tokens
        self.max_prompt_tokens = max(self.max_prompt_tokens, call_prompt_tokens)

        completion_details = getattr(usage, "completion_tokens_details", None)
        call_reasoning_tokens = _coerce_int(
            _get_attr_or_item(completion_details, "reasoning_tokens")
        )
        self.reasoning_tokens += call_reasoning_tokens

        prompt_details = getattr(usage, "prompt_tokens_details", None)
        # LiteLLM normalizes Anthropic's `cache_read_input_tokens` →
        # `prompt_tokens_details.cached_tokens` and
        # `cache_creation_input_tokens` →
        # `prompt_tokens_details.cache_creation_tokens`. We also fall back to
        # the flat Anthropic-shaped attributes in case a backend hasn't been
        # routed through the normalizer.
        cached = _get_attr_or_item(prompt_details, "cached_tokens")
        if not cached:
            cached = getattr(usage, "cache_read_input_tokens", None)
        call_cached_tokens = _coerce_int(cached)
        self.cached_tokens += call_cached_tokens

        cache_creation = _get_attr_or_item(prompt_details, "cache_creation_tokens")
        if not cache_creation:
            cache_creation = getattr(usage, "cache_creation_input_tokens", None)
        call_cache_creation_tokens = _coerce_int(cache_creation)
        self.cache_creation_tokens += call_cache_creation_tokens

        self.call_log.append(
            {
                "prompt_tokens": call_prompt_tokens,
                "completion_tokens": call_completion_tokens,
                "total_tokens": call_prompt_tokens + call_completion_tokens,
                "reasoning_tokens": call_reasoning_tokens,
                "cached_tokens": call_cached_tokens,
                "cache_creation_tokens": call_cache_creation_tokens,
            }
        )

        if self._breakdown:
            self._enrich_last_call(response, call_prompt_tokens)
        self._prev_prompt_tokens = call_prompt_tokens

    def track_from_dict(self, response_dict: dict[str, Any]) -> None:
        """Extract and accumulate usage from a response dictionary (e.g., Responses API).

        Handles both OpenAI Responses API format and standard completion format.
        """
        self._capture_provider(response_dict)
        usage = response_dict.get("usage")
        if usage is None:
            return

        prompt_tokens = _coerce_int(
            usage.get("prompt_tokens") or usage.get("input_tokens")
        )
        completion_tokens = _coerce_int(
            usage.get("completion_tokens") or usage.get("output_tokens")
        )

        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.final_answer_tokens = completion_tokens
        self.max_prompt_tokens = max(self.max_prompt_tokens, prompt_tokens)

        completion_details = usage.get("completion_tokens_details") or {}
        # Responses API exposes reasoning tokens as `output_tokens_details.reasoning_tokens`.
        output_details = usage.get("output_tokens_details") or {}
        call_reasoning_tokens = _coerce_int(
            completion_details.get("reasoning_tokens")
            or output_details.get("reasoning_tokens")
        )
        self.reasoning_tokens += call_reasoning_tokens

        prompt_details = usage.get("prompt_tokens_details") or {}
        input_details = usage.get("input_tokens_details") or {}
        cached = (
            prompt_details.get("cached_tokens")
            or input_details.get("cached_tokens")
            or usage.get("cache_read_input_tokens")
        )
        call_cached_tokens = _coerce_int(cached)
        self.cached_tokens += call_cached_tokens

        cache_creation = prompt_details.get("cache_creation_tokens") or usage.get(
            "cache_creation_input_tokens"
        )
        call_cache_creation_tokens = _coerce_int(cache_creation)
        self.cache_creation_tokens += call_cache_creation_tokens

        self.call_log.append(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "reasoning_tokens": call_reasoning_tokens,
                "cached_tokens": call_cached_tokens,
                "cache_creation_tokens": call_cache_creation_tokens,
            }
        )

        if self._breakdown:
            self._enrich_last_call_from_dict(response_dict, prompt_tokens)
        self._prev_prompt_tokens = prompt_tokens

    def _tok(self, text: Any) -> int:
        """Best-effort token count for a text fragment using the call's model."""
        if not text:
            return 0
        if not isinstance(text, str):
            text = json.dumps(text)
        for model in (self._model, "gpt-4o"):
            try:
                return litellm.token_counter(model=model, text=text)
            except Exception:
                continue
        return 0

    @staticmethod
    def _content_text(content: Any) -> str:
        """Flatten message content (str or list of blocks) to plain text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        return ""

    @staticmethod
    def _thinking_text(msg: Any) -> str:
        """Extract a message's extended-thinking text (Anthropic / reasoning models).

        Prefers raw `thinking_blocks`; falls back to `reasoning_content`. Never
        sums both (LiteLLM may populate both with the same content, which would
        double-count).
        """
        if msg is None:
            return ""
        blocks = getattr(msg, "thinking_blocks", None)
        if isinstance(blocks, list):
            parts = [
                b["thinking"]
                for b in blocks
                if isinstance(b, dict) and b.get("thinking")
            ]
            if parts:
                return " ".join(parts)
        rc = getattr(msg, "reasoning_content", None)
        return rc if isinstance(rc, str) else ""

    def _tool_call_input_tokens(self, tool_calls: Any) -> int:
        """Count tokens the model spent emitting its tool call(s).

        Counts the function name plus the raw argument string exactly as the
        model emitted it. Critically, a string `arguments` is tokenized as-is
        (never re-serialized) — re-`json.dumps`-ing it would change whitespace
        and drift the count. Residual per-call structural framing the provider
        adds is not separately attributable and is the known small drift.
        """
        total = 0
        for tc in tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            name = getattr(fn, "name", None)
            if name:
                total += self._tok(name)
            args = getattr(fn, "arguments", None)
            if isinstance(args, str):
                total += self._tok(args)
            elif args is not None:
                total += self._tok(json.dumps(args))
        return total

    def _enrich_last_call(
        self, response: ModelResponse, call_prompt_tokens: int
    ) -> None:
        """Add derived per-step splits to the latest call_log entry.

        Fills visible-output, tool-call-argument, reasoning-summary, and
        marginal-input token counts. tool_call_output_tokens is seeded to 0 and
        populated later via track_tool_output once tool results return.
        """
        entry = self.call_log[-1]
        msg: Any = None
        try:
            choices = getattr(response, "choices", None)
            if choices:
                msg = choices[0].message
        except Exception:
            msg = None
        content = getattr(msg, "content", None) if msg is not None else None
        reasoning = getattr(msg, "reasoning_content", None) if msg is not None else None
        tool_calls = getattr(msg, "tool_calls", None) if msg is not None else None
        tool_input = self._tool_call_input_tokens(tool_calls)
        usage = getattr(response, "usage", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        entry["user_output_tokens"] = self._tok(self._content_text(content))
        entry["reasoning_summary_tokens"] = self._tok(reasoning)
        # Anthropic folds thinking into output and often omits a reasoning count
        # (shows 0). When the provider reported 0 but the model emitted thinking,
        # derive the actual reasoning tokens from the thinking content so it is
        # non-zero for thinking-enabled runs (e.g. Opus-4.8).
        if entry.get("reasoning_tokens", 0) == 0:
            thinking_text = self._thinking_text(msg)
            if thinking_text:
                # The estimate uses the OpenAI tokenizer as a fallback for models
                # without a known tokenizer (GLM/DeepSeek via baseten), so it can
                # exceed the provider's exact completion count. Clamp to the room
                # left after visible output and tool-call input to preserve the
                # identity reasoning + visible + tool_input <= completion.
                room = max(
                    _coerce_int(entry.get("completion_tokens", 0))
                    - entry["user_output_tokens"]
                    - tool_input,
                    0,
                )
                derived = min(self._tok(thinking_text), room)
                entry["reasoning_tokens"] = derived
                self.reasoning_tokens += derived
        entry["tool_call_input_tokens"] = tool_input
        entry["tool_call_output_tokens"] = 0
        entry["tool_call_output_image_tokens"] = 0
        entry["tool_call_output_image_count"] = 0
        entry["final_answer_tokens"] = 0
        # Provider-reported input image tokens (Gemini surfaces these; OpenAI /
        # Anthropic fold them into prompt_tokens with no split).
        entry["image_tokens"] = _coerce_int(
            _get_attr_or_item(prompt_details, "image_tokens")
        )
        entry["marginal_input_tokens"] = max(
            call_prompt_tokens - self._prev_prompt_tokens, 0
        )
        # Attribute compactions since the last call to this row so identity
        # checks can exclude it and the per-row counts reconcile with
        # compaction_count (sum of compactions_before over rows).
        entry["compactions_before"] = self._pending_compactions
        entry["compacted"] = self._pending_compactions > 0
        self._pending_compactions = 0

    @staticmethod
    def _responses_visible_text(output_items: Any) -> str:
        """Join visible output_text from Responses API ``message`` items."""
        parts: list[str] = []
        for item in output_items or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for c in item.get("content", []) or []:
                if (
                    isinstance(c, dict)
                    and c.get("type") == "output_text"
                    and c.get("text")
                ):
                    parts.append(c["text"])
        return " ".join(parts)

    @staticmethod
    def _responses_reasoning_summary(output_items: Any) -> str:
        """Join reasoning-summary text from Responses API ``reasoning`` items.

        Prefers ``summary`` (summary_text); falls back to ``content``
        (reasoning_text). This is the summary the provider exposes, not the raw
        chain of thought.
        """
        parts: list[str] = []
        for item in output_items or []:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            # Per-item: prefer this item's summary_text, else fall back to this
            # item's reasoning_text. The fallback must be scoped to the item, not
            # the global accumulator, or items with only reasoning_text are
            # dropped once any earlier item supplied a summary.
            item_parts: list[str] = []
            for s in item.get("summary", []) or []:
                if (
                    isinstance(s, dict)
                    and s.get("type") == "summary_text"
                    and s.get("text")
                ):
                    item_parts.append(s["text"])
            if not item_parts:
                for c in item.get("content", []) or []:
                    if (
                        isinstance(c, dict)
                        and c.get("type") == "reasoning_text"
                        and c.get("text")
                    ):
                        item_parts.append(c["text"])
            parts.extend(item_parts)
        return " ".join(parts)

    def _responses_tool_input_tokens(self, output_items: Any) -> int:
        """Count tokens the model spent emitting Responses API tool calls.

        Counts the function name plus the raw argument string as emitted; a
        string ``arguments`` is tokenized as-is (never re-serialized).
        """
        total = 0
        for item in output_items or []:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            name = item.get("name")
            if name:
                total += self._tok(name)
            args = item.get("arguments")
            if isinstance(args, str):
                total += self._tok(args)
            elif args is not None:
                total += self._tok(json.dumps(args))
        return total

    def _enrich_last_call_from_dict(
        self, response_dict: dict[str, Any], call_prompt_tokens: int
    ) -> None:
        """Add derived per-step splits to the latest call_log entry (Responses API).

        Mirrors _enrich_last_call for the Responses API response shape. Unlike
        the chat path, reasoning_tokens is left provider-sourced: the Responses
        API reports the exact count via ``output_tokens_details.reasoning_tokens``
        (already read in track_from_dict), so no summary-based derivation is
        applied (that would undercount the real reasoning).
        """
        entry = self.call_log[-1]
        output_items = response_dict.get("output") or []
        entry["user_output_tokens"] = self._tok(
            self._responses_visible_text(output_items)
        )
        entry["reasoning_summary_tokens"] = self._tok(
            self._responses_reasoning_summary(output_items)
        )
        entry["tool_call_input_tokens"] = self._responses_tool_input_tokens(
            output_items
        )
        entry["tool_call_output_tokens"] = 0
        entry["tool_call_output_image_tokens"] = 0
        entry["tool_call_output_image_count"] = 0
        entry["final_answer_tokens"] = 0
        usage = response_dict.get("usage") or {}
        image_details = (
            usage.get("input_tokens_details")
            or usage.get("prompt_tokens_details")
            or {}
        )
        entry["image_tokens"] = _coerce_int(image_details.get("image_tokens"))
        entry["marginal_input_tokens"] = max(
            call_prompt_tokens - self._prev_prompt_tokens, 0
        )
        entry["compactions_before"] = self._pending_compactions
        entry["compacted"] = self._pending_compactions > 0
        self._pending_compactions = 0

    def track_tool_output(self, text: Any) -> None:
        """Accumulate tool-result tokens onto the current step's call_log entry.

        No-op unless breakdown tracking is on and at least one call is logged.
        """
        if not self._breakdown or not self.call_log:
            return
        self.call_log[-1]["tool_call_output_tokens"] = self.call_log[-1].get(
            "tool_call_output_tokens", 0
        ) + self._tok(text)

    @staticmethod
    def _image_dims_from_data_uri(uri: Any) -> tuple[int, int] | None:
        """Parse (width, height) from a base64 image data URI, stdlib only.

        Handles PNG / JPEG / GIF / WEBP headers. Returns None on any malformed or
        unsupported input — the data URI is untrusted tool output, so every parse
        path is defensive.
        """
        if not isinstance(uri, str) or "base64," not in uri:
            return None
        try:
            b64 = uri.split("base64,", 1)[1]
            head = base64.b64decode(b64[:1024])  # header is enough for most formats
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                fmt = head[12:16]
                if fmt == b"VP8X":
                    w = 1 + int.from_bytes(head[24:27], "little")
                    h = 1 + int.from_bytes(head[27:30], "little")
                    return w, h
                if fmt == b"VP8 ":
                    w = struct.unpack("<H", head[26:28])[0] & 0x3FFF
                    h = struct.unpack("<H", head[28:30])[0] & 0x3FFF
                    return int(w), int(h)
            if head[:2] == b"\xff\xd8":  # JPEG — scan SOF markers in a capped prefix
                # The data URI is untrusted tool output; cap the decode so a huge
                # JPEG can't force large allocations. The SOF marker carrying the
                # dimensions sits near the start (after APP/EXIF segments); 256KB
                # covers it for any normal plot/screenshot. Slice to a multiple of
                # 4 so base64 padding stays valid; unfound -> None (count-only).
                cap_b64 = (256 * 1024 // 3) * 4
                data = base64.b64decode(b64[:cap_b64])
                i, n = 2, len(data)
                while i + 9 < n:
                    if data[i] != 0xFF:
                        i += 1
                        continue
                    marker = data[i + 1]
                    if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                        h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                        return int(w), int(h)
                    i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
        except Exception:
            return None
        return None

    def _image_tokens(self, width: int, height: int) -> int:
        """Native-image token cost from pixel dims, per provider (verified
        formulas). 0 for models with no registered formula (NA).

        Family is decided provider-first (the provider is what actually
        tokenizes the image), falling back to model-name keywords when the
        provider wasn't captured. ``vertex_ai`` is intentionally NOT treated as
        Gemini: it hosts Claude, Gemini, and third-party models, and its model
        names always carry the family (``gemini-...`` / ``claude-...``), so the
        name keywords resolve it without mis-costing a non-Gemini Vertex model.
        """
        if width <= 0 or height <= 0:
            return 0
        model = (self._model or "").lower()
        provider = (self._provider or "").lower()

        is_anthropic = (
            provider == "anthropic" or "claude" in model or "anthropic" in model
        )
        is_openai = not is_anthropic and (
            provider == "openai" or "gpt-5" in model or "openai" in model
        )
        is_gemini = (
            not is_anthropic
            and not is_openai
            and (provider == "gemini" or "gemini" in model or "ajax" in model)
        )

        if is_anthropic:
            # Capped tile count. The image's own token cost is the tile count
            # (verified via count_tokens); the ~80-token tool_result framing is
            # the wrapper's cost, not the image's, so it is not added here.
            tiles = math.ceil(width / 28) * math.ceil(height / 28)
            return min(tiles, 4784)
        if is_openai:
            return min(
                math.ceil(1.2 * math.ceil(width / 32) * math.ceil(height / 32)), 12000
            )
        if is_gemini:
            # 258 tokens per 768px tile (no cap); a large image spans many tiles.
            tiles = math.ceil(width / 768) * math.ceil(height / 768)
            return 258 * max(tiles, 1)
        return 0

    def track_tool_output_image(self, image: Any) -> None:
        """Add a tool-result image's token cost to the current step.

        ``image`` is a base64 data URI (or a pre-parsed ``(w, h)`` tuple). Native
        images are elided from the stored trajectory, so this must run live while
        the data URI is still present. No-op unless breakdown tracking is on and a
        call is logged.
        """
        if not self._breakdown or not self.call_log:
            return
        if isinstance(image, tuple) and len(image) == 2:
            dims: tuple[int, int] | None = image
        elif isinstance(image, str) and image.startswith("data:"):
            dims = self._image_dims_from_data_uri(image)
        else:
            return  # not a real image
        entry = self.call_log[-1]
        # Count every image we see, even when its token cost is unknown (provider
        # has no registered formula, e.g. Nemotron / GLM-4.6V) — so an uncounted
        # image reads as "present, uncounted" instead of a silent 0.
        entry["tool_call_output_image_count"] = (
            entry.get("tool_call_output_image_count", 0) + 1
        )
        if dims:
            entry["tool_call_output_image_tokens"] = entry.get(
                "tool_call_output_image_tokens", 0
            ) + self._image_tokens(int(dims[0]), int(dims[1]))

    def track_final_answer(self, text: Any) -> None:
        """Record the isolated final-answer content tokens on the current step.

        Distinct from the top-level ``final_answer_tokens`` aggregate (the last
        call's full completion); this is the parsed answer content for the step
        that emitted it. No-op unless breakdown tracking is on.
        """
        if not self._breakdown or not self.call_log:
            return
        self.call_log[-1]["final_answer_tokens"] = self._tok(text)

    def to_dict(self) -> dict[str, Any]:
        """Return accumulated usage as a dict for AgentTrajectoryOutput."""
        result: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "final_answer_tokens": self.final_answer_tokens,
            "max_prompt_tokens": self.max_prompt_tokens,
            "compaction_count": self.compaction_count,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "call_log": self.call_log,
        }
        return result
