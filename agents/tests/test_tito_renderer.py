"""Pure resolution-helper tests for tito_renderer (no transformers/renderers)."""

from __future__ import annotations

import pytest

from runner.utils import tito_renderer


def test_resolve_hf_id_strips_provider_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERCOR_POLICY_HF_ID", raising=False)
    assert tito_renderer.resolve_hf_id("hosted_vllm/Qwen/Qwen3-8B") == "Qwen/Qwen3-8B"
    assert tito_renderer.resolve_hf_id("responses/openai/Qwen/Qwen3-4B") == (
        "Qwen/Qwen3-4B"
    )


def test_resolve_hf_id_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERCOR_POLICY_HF_ID", "Qwen/Qwen3-32B")
    assert tito_renderer.resolve_hf_id("fireworks_ai/qwen3p6-27b") == "Qwen/Qwen3-32B"


def test_is_default_renderer_detects_generic_fallback() -> None:
    class DefaultRenderer:  # name matches renderers' generic fallback
        pass

    class Qwen3Renderer:
        pass

    assert tito_renderer._is_default_renderer(DefaultRenderer())
    assert not tito_renderer._is_default_renderer(Qwen3Renderer())


def test_validate_hf_id_rejects_default_renderer_fallback() -> None:
    with pytest.raises(RuntimeError, match="exact text-only model"):
        tito_renderer.validate_hf_id("some-org/unregistered-fine-tune")


def test_as_ids_accepts_list_and_rendered_tokens() -> None:
    from types import SimpleNamespace

    # render_ids returns a plain list; bridge_to_next_turn returns a
    # RenderedTokens object exposing .token_ids — accept both.
    assert tito_renderer._as_ids([1, 2, 3]) == [1, 2, 3]
    assert tito_renderer._as_ids(SimpleNamespace(token_ids=[4, 5])) == [4, 5]


def test_registered_text_models_are_exact_and_exclude_multimodal() -> None:
    supported = tito_renderer.registered_text_model_ids()
    assert "Qwen/Qwen3-8B" in supported
    assert "moonshotai/Kimi-K2-Instruct" in supported
    assert "openai/gpt-oss-20b" in supported
    assert "Qwen/Qwen3-VL-8B-Instruct" not in supported
    assert "Qwen/Qwen3.5-9B" not in supported
    assert len(supported) == 39


def test_wrapper_preserves_explicit_empty_tools() -> None:
    class FakeRenderer:
        def __init__(self) -> None:
            self.seen: list[object] = []

        def render(self, messages, *, tools=None, add_generation_prompt=False):
            from types import SimpleNamespace

            self.seen.append(tools)
            return SimpleNamespace(token_ids=[1], multi_modal_data=None)

        def bridge_to_next_turn(self, prompt, completion, messages, *, tools=None):
            from types import SimpleNamespace

            self.seen.append(tools)
            return SimpleNamespace(
                token_ids=[*prompt, *completion, 2], multi_modal_data=None
            )

        def parse_response(self, completion, *, tools=None):
            from types import SimpleNamespace

            self.seen.append(tools)
            return SimpleNamespace(content="ok", reasoning_content=None, tool_calls=[])

    wrapper = object.__new__(tito_renderer.TitoRenderer)
    fake = FakeRenderer()
    wrapper.hf_id = "Qwen/Qwen3-8B"
    wrapper.chat_template_kwargs = {}
    wrapper._renderer = fake
    assert wrapper.render_ids([{"role": "user", "content": "hi"}], []) == [1]
    assert wrapper.bridge([1], [2], [{"role": "tool", "content": "x"}], []) == [
        1,
        2,
        2,
    ]
    wrapper.parse([3], [])
    assert fake.seen == [[], [], []]


def test_normalize_messages_flattens_text_parts_and_rejects_media() -> None:
    assert tito_renderer.normalize_messages(
        [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    ) == [{"role": "user", "content": "hello"}]
    with pytest.raises(ValueError, match="text-only"):
        tito_renderer.normalize_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png"}}
                    ],
                }
            ]
        )
