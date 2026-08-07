"""Buffer state machine + env gate tests for flat TITO capture.

Uses a fake renderer and a mocked token-in transport — no network, no tokenizer
download, no renderers/transformers import.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from runner.utils import llm, tito_capture, tito_renderer, tito_session

_API_BASE = "https://my-policy.modal.run/v1"


class _FakeRenderer:
    """Deterministic stand-in for TitoRenderer.

    ``render_ids`` returns a fixed first prompt; ``bridge`` extends
    (prev_prompt + prev_completion) with the next delta, or ``None`` when
    ``bridge_none`` is set (simulating a non-append-only turn).
    """

    def __init__(
        self,
        prompt1: list[int],
        deltas: list[list[int]],
        parsed: Any,
        parse_errors: list[str] | None = None,
    ):
        self.prompt1 = prompt1
        self.deltas = list(deltas)
        self.parsed = parsed
        self.parse_errors = list(parse_errors or [])
        self.bridge_none = False

    def render_ids(self, messages: Any, tools: Any) -> list[int]:
        return list(self.prompt1)

    def bridge(
        self, prev_p: list[int], prev_c: list[int], new_msgs: Any, tools: Any
    ) -> list[int] | None:
        if self.bridge_none:
            return None
        return list(prev_p) + list(prev_c) + list(self.deltas.pop(0))

    def parse(
        self, resp_ids: list[int], tools: Any = None
    ) -> tito_renderer.TitoParsedResponse:
        content, reasoning, calls = self.parsed
        parsed_calls: list[tito_renderer.TitoParsedToolCall] = []
        for call in calls or []:
            function = call.get("function", call)
            parsed_calls.append(
                tito_renderer.TitoParsedToolCall(
                    id=call.get("id"),
                    name=function["name"],
                    arguments=function.get("arguments"),
                )
            )
        return tito_renderer.TitoParsedResponse(
            content=content,
            reasoning_content=reasoning,
            tool_calls=parsed_calls,
            parse_errors=self.parse_errors,
        )


def _session(renderer: _FakeRenderer) -> tito_session.TitoCaptureSession:
    s = tito_session.TitoCaptureSession(
        policy_model="fireworks_ai/qwen3-8b",
        hf_id="Qwen/Qwen3-8B",
        api_base=_API_BASE,
        api_key="k",
        default_max_tokens=8192,
    )
    # Inject the fake, skipping the real (network) renderer build. cast to Any
    # keeps basedpyright happy (no invalid-cast); plain assignment keeps ruff B010.
    s._renderer = cast(Any, renderer)
    return s


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch, results: list[tuple[list[int], list[float]]]
) -> None:
    it = iter(results)

    async def _fake(**kwargs: Any) -> tito_capture.TokenCompletion:
        response_ids, logprobs = next(it)
        prompt_ids = list(kwargs["prompt_token_ids"])
        return tito_capture.TokenCompletion(
            prompt_token_ids=prompt_ids,
            completion_token_ids=response_ids,
            token_logprobs=logprobs,
            text="",
            finish_reason="stop",
            native_finish_reason="stop",
            usage={
                "prompt_tokens": len(prompt_ids),
                "completion_tokens": len(response_ids),
                "total_tokens": len(prompt_ids) + len(response_ids),
            },
            request_id=None,
            model=None,
        )

    monkeypatch.setattr(tito_capture, "generate_tokens", _fake)


def _completion(
    kwargs: dict[str, Any], response_ids: list[int], logprobs: list[float]
) -> tito_capture.TokenCompletion:
    prompt_ids = list(kwargs["prompt_token_ids"])
    return tito_capture.TokenCompletion(
        prompt_token_ids=prompt_ids,
        completion_token_ids=response_ids,
        token_logprobs=logprobs,
        text="",
        finish_reason="stop",
        native_finish_reason="stop",
        usage={
            "prompt_tokens": len(prompt_ids),
            "completion_tokens": len(response_ids),
            "total_tokens": len(prompt_ids) + len(response_ids),
        },
        request_id=None,
        model=None,
    )


# --- flat buffer assembly ---------------------------------------------------


async def test_flat_buffer_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    renderer = _FakeRenderer(prompt1=[1, 2, 3], deltas=[[6]], parsed=("hi", None, None))
    _mock_transport(monkeypatch, [([4, 5], [-0.1, -0.2]), ([7], [-0.3])])
    s = _session(renderer)

    first = [{"role": "user", "content": "m0"}]
    await s.generate("fireworks_ai/qwen3-8b", first, None, {}, timeout=30)
    # Harness echoes our assistant turn + a tool result before the next call.
    await s.generate(
        "fireworks_ai/qwen3-8b",
        [
            *first,
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "content": "tool1"},
        ],
        None,
        {},
        timeout=30,
    )

    out = s.output_dict()
    assert out["tito_version"] == 1
    assert out["tito_complete"] is True
    assert out["token_ids"] == [1, 2, 3, 4, 5, 6, 7]
    # loss_mask=1 only on sampled tokens (4,5 and 7); prompt+bridge are 0.
    assert out["loss_mask"] == [0, 0, 0, 1, 1, 0, 1]
    assert out["logprobs"] == [0.0, 0.0, 0.0, -0.1, -0.2, 0.0, -0.3]


async def test_non_append_only_marks_incomplete_and_stops_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _FakeRenderer(prompt1=[1, 2, 3], deltas=[], parsed=("hi", None, None))
    _mock_transport(monkeypatch, [([4, 5], [-0.1, -0.2]), ([7], [-0.3])])
    s = _session(renderer)

    await s.generate("fireworks_ai/qwen3-8b", ["m0"], None, {}, timeout=30)
    renderer.bridge_none = True  # next turn can't be safely extended
    await s.generate(
        "fireworks_ai/qwen3-8b", ["m0", "assistant1", "tool1"], None, {}, timeout=30
    )

    out = s.output_dict()
    assert out["tito_complete"] is False
    assert out["tito_error"]
    # Buffer frozen at turn 1 — never fabricated past the break.
    assert out["token_ids"] == [1, 2, 3, 4, 5]
    assert out["loss_mask"] == [0, 0, 0, 1, 1]


async def test_history_shrink_marks_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _FakeRenderer(prompt1=[1, 2, 3], deltas=[], parsed=("hi", None, None))
    _mock_transport(monkeypatch, [([4, 5], [-0.1, -0.2]), ([7], [-0.3])])
    s = _session(renderer)
    await s.generate("fireworks_ai/qwen3-8b", ["a", "b", "c"], None, {}, timeout=30)
    # Message list shrank (a summarizer rewrote history) — not a safe extension.
    await s.generate("fireworks_ai/qwen3-8b", ["short"], None, {}, timeout=30)
    assert s.output_dict()["tito_complete"] is False


async def test_transient_request_suffix_cannot_skip_a_real_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _FakeRenderer(prompt1=[1, 2, 3], deltas=[[6]], parsed=("hi", None, None))
    _mock_transport(monkeypatch, [([4, 5], [-0.1, -0.2]), ([7], [-0.3])])
    s = _session(renderer)
    first = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "user", "content": "transient-1"},
    ]
    await s.generate("fireworks_ai/qwen3-8b", first, None, {}, timeout=30)
    second = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "real-result"},
        {"role": "user", "content": "transient-2"},
    ]
    await s.generate("fireworks_ai/qwen3-8b", second, None, {}, timeout=30)
    assert s.output_dict()["tito_complete"] is False


async def test_response_contains_exact_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    renderer = _FakeRenderer(prompt1=[1, 2, 3], deltas=[], parsed=("hi", None, None))
    _mock_transport(monkeypatch, [([4, 5], [-0.1, -0.2])])
    response = await _session(renderer).generate(
        "fireworks_ai/qwen3-8b", [{"role": "user", "content": "hi"}], None, {}, 30
    )
    usage = getattr(response, "usage", None)
    assert usage is not None
    assert usage.prompt_tokens == 3
    assert usage.completion_tokens == 2


async def test_concurrent_calls_freeze_without_interleaving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _FakeRenderer(prompt1=[1], deltas=[], parsed=("hi", None, None))
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _fake(**kwargs: Any) -> tito_capture.TokenCompletion:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return _completion(kwargs, [calls + 1], [-0.1])

    monkeypatch.setattr(tito_capture, "generate_tokens", _fake)
    s = _session(renderer)
    first = asyncio.create_task(
        s.generate(
            "fireworks_ai/qwen3-8b",
            [{"role": "user", "content": "first"}],
            None,
            {},
            30,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        s.generate(
            "fireworks_ai/qwen3-8b",
            [{"role": "user", "content": "second"}],
            None,
            {},
            30,
        )
    )
    release.set()
    await asyncio.gather(first, second)
    out = s.output_dict()
    assert out["tito_complete"] is False
    assert out["token_ids"] == [1, 2]


async def test_responses_facade_uses_shared_parsed_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _FakeRenderer(prompt1=[1, 2], deltas=[], parsed=("hello", "why", None))
    _mock_transport(monkeypatch, [([3], [-0.2])])
    response = await _session(renderer).generate_responses(
        "fireworks_ai/qwen3-8b",
        [{"role": "developer", "content": "system"}, {"role": "user", "content": "hi"}],
        [],
        {},
        30,
    )
    assert response.status == "completed"
    assert response.usage.input_tokens == 2
    assert response.usage.output_tokens == 1
    assert [item["type"] for item in response.output] == ["reasoning", "message"]


async def test_responses_reasoning_and_tool_echo_remains_linear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = [{"id": "call_1", "function": {"name": "search", "arguments": {"q": "x"}}}]
    renderer = _FakeRenderer(prompt1=[1, 2], deltas=[[5]], parsed=(None, "why", calls))
    _mock_transport(monkeypatch, [([3, 4], [-0.1, -0.2]), ([6], [-0.3])])
    session = _session(renderer)
    first_messages = [
        {"role": "developer", "content": "system"},
        {"role": "user", "content": "hi"},
    ]
    first = await session.generate_responses(
        "fireworks_ai/qwen3-8b", first_messages, [], {}, 30
    )
    function_call = next(
        item for item in first.output if item["type"] == "function_call"
    )
    next_input = [
        {"role": "developer", "content": "system"},
        {"role": "user", "content": "hi"},
        {
            "type": "function_call",
            "id": function_call["id"],
            "call_id": function_call["call_id"],
            "name": function_call["name"],
            "arguments": function_call["arguments"],
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        },
    ]
    await session.generate_responses("fireworks_ai/qwen3-8b", next_input, [], {}, 30)
    assert session.output_dict()["tito_complete"] is True


async def test_central_responses_path_diverts_to_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _FakeRenderer(prompt1=[1], deltas=[], parsed=("hello", None, None))
    _mock_transport(monkeypatch, [([2], [-0.1])])
    session = _session(renderer)
    token = tito_session.set_active_session(session)

    async def _unexpected(**_: Any) -> Any:
        raise AssertionError("native Responses transport must not run")

    monkeypatch.setattr(llm, "aresponses", _unexpected)
    try:
        response = await llm.call_responses_api(
            "fireworks_ai/qwen3-8b",
            [{"role": "user", "content": "hi"}],
            [],
            30,
            {},
            stream=True,
        )
    finally:
        tito_session.reset_active_session(token)
    assert response.output[0]["content"][0]["text"] == "hello"
    assert tito_session.get_active_session() is None


async def test_parse_ambiguity_freezes_after_recording_exact_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _FakeRenderer(
        prompt1=[1],
        deltas=[],
        parsed=("", None, None),
        parse_errors=["invalid_json"],
    )
    _mock_transport(monkeypatch, [([2], [-0.1])])
    session = _session(renderer)
    await session.generate(
        "fireworks_ai/qwen3-8b",
        [{"role": "user", "content": "hi"}],
        None,
        {},
        30,
    )
    out = session.output_dict()
    assert out["token_ids"] == [1, 2]
    assert out["tito_complete"] is False
    assert "parse" in out["tito_error"]


async def test_response_reconstructed_with_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_calls = [{"id": "c1", "function": {"name": "search", "arguments": {"q": "x"}}}]
    renderer = _FakeRenderer(
        prompt1=[1], deltas=[], parsed=(None, "thinking", tool_calls)
    )
    _mock_transport(monkeypatch, [([2, 3], [-0.1, -0.2])])
    s = _session(renderer)

    resp = await s.generate("fireworks_ai/qwen3-8b", ["m0"], None, {}, timeout=30)
    msg = resp.choices[0].message
    assert resp.choices[0].finish_reason == "tool_calls"
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].function.name == "search"
    # arguments serialized to a JSON string.
    assert msg.tool_calls[0].function.arguments == '{"q": "x"}'


async def test_none_output_limit_uses_session_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> tito_capture.TokenCompletion:
        seen.update(kwargs)
        return _completion(kwargs, [2], [-0.1])

    monkeypatch.setattr(tito_capture, "generate_tokens", _fake)
    session = _session(_FakeRenderer(prompt1=[1], deltas=[], parsed=("hi", None, None)))
    await session.generate(
        "fireworks_ai/qwen3-8b",
        [{"role": "user", "content": "hi"}],
        None,
        {"max_output_tokens": None},
        30,
    )
    assert seen["sampling_args"]["max_tokens"] == 8192


async def test_max_output_tokens_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> tito_capture.TokenCompletion:
        seen.update(kwargs)
        return _completion(kwargs, [2], [-0.1])

    monkeypatch.setattr(tito_capture, "generate_tokens", _fake)
    s = _session(_FakeRenderer(prompt1=[1], deltas=[], parsed=("hi", None, None)))
    await s.generate(
        "fireworks_ai/qwen3-8b", ["m0"], None, {"max_output_tokens": 123}, timeout=30
    )
    assert seen["sampling_args"]["max_tokens"] == 123
    assert "max_output_tokens" not in seen["sampling_args"]


# --- handles + output_dict edges -------------------------------------------


def test_handles_gates_to_policy_model() -> None:
    s = _session(_FakeRenderer([1], [], ("", None, None)))
    assert s.handles("fireworks_ai/qwen3-8b")
    assert s.handles("responses/fireworks_ai/qwen3-8b")
    assert not s.handles("anthropic/claude-helper")


def test_output_dict_marks_no_call_incomplete() -> None:
    s = _session(_FakeRenderer([1], [], ("", None, None)))
    assert s.output_dict()["tito_complete"] is False
    assert "no matching" in s.output_dict()["tito_error"]


def test_nest_in_output_preserves_harness_keys() -> None:
    tito_payload = {
        "tito_version": 1,
        "tito_complete": True,
        "token_ids": [1, 2],
        "loss_mask": [0, 1],
        "logprobs": [0.0, -0.1],
    }
    merged = tito_session.nest_in_output({"harness_key": "keep"}, tito_payload)
    assert merged["harness_key"] == "keep"
    assert merged["tito"] == tito_payload
    assert "token_ids" not in merged


def test_nest_in_output_handles_none_harness_output() -> None:
    merged = tito_session.nest_in_output(
        None, {"tito_version": 1, "tito_complete": False}
    )
    assert merged == {"tito": {"tito_version": 1, "tito_complete": False}}


# --- tool-call adapter ------------------------------------------------------


def test_tool_calls_adapter_serializes_parsed_calls() -> None:
    calls = [tito_renderer.TitoParsedToolCall(id=None, name="f", arguments={"a": 1})]
    converted = tito_session._tool_calls_to_dicts(calls)
    assert converted and converted[0]["id"]
    assert converted[0]["function"] == {"name": "f", "arguments": '{"a": 1}'}
    assert tito_session._tool_calls_to_dicts([]) is None


# --- env gate + family resolution ------------------------------------------


def test_session_from_env_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERCOR_TITO_CAPTURE", raising=False)
    assert tito_session.session_from_env(policy_model="Qwen/Qwen3-8B") is None


def test_session_from_env_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERCOR_TITO_CAPTURE", "true")
    monkeypatch.delenv("MERCOR_POLICY_API_BASE", raising=False)
    with pytest.raises(RuntimeError, match="MERCOR_POLICY_API_BASE"):
        tito_session.session_from_env(policy_model="Qwen/Qwen3-8B")


def test_session_from_env_requires_registered_text_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERCOR_TITO_CAPTURE", "true")
    monkeypatch.setenv("MERCOR_POLICY_API_BASE", _API_BASE)
    monkeypatch.delenv("MERCOR_POLICY_HF_ID", raising=False)
    with pytest.raises(RuntimeError, match="exact text-only model"):
        tito_session.session_from_env(policy_model="meta-llama/Llama-3.1-8B")
