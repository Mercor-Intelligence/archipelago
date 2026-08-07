"""Token-in transport + extraction tests for flat TITO capture (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from runner.utils import tito_capture


def _resp(
    *,
    resp_ids: list[int] | None,
    token_logprobs: list[Any] | None,  # list[Any] so the null-logprob case type-checks
    use_psf: bool = False,
) -> Any:
    if use_psf:
        choice = SimpleNamespace(
            provider_specific_fields={"token_ids": resp_ids},
            logprobs=SimpleNamespace(token_logprobs=token_logprobs),
        )
    else:
        choice = SimpleNamespace(
            provider_specific_fields=None,
            token_ids=resp_ids,
            logprobs=SimpleNamespace(token_logprobs=token_logprobs),
        )
    return SimpleNamespace(choices=[choice])


def test_extract_reads_token_ids_and_logprobs() -> None:
    resp_ids, logprobs = tito_capture.extract_tokens(
        _resp(resp_ids=[4, 5], token_logprobs=[-0.1, -0.2])
    )
    assert resp_ids == [4, 5]
    assert logprobs == [-0.1, -0.2]


def test_extract_reads_from_provider_specific_fields() -> None:
    resp_ids, _ = tito_capture.extract_tokens(
        _resp(resp_ids=[9], token_logprobs=[-0.3], use_psf=True)
    )
    assert resp_ids == [9]


def test_extract_no_token_ids_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="capture-capable"):
        tito_capture.extract_tokens(_resp(resp_ids=None, token_logprobs=None))


def test_extract_misaligned_logprobs_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="logprobs unusable"):
        tito_capture.extract_tokens(_resp(resp_ids=[4, 5], token_logprobs=[-0.1]))


def test_extract_null_logprobs_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="null"):
        tito_capture.extract_tokens(_resp(resp_ids=[4], token_logprobs=[None]))


def test_extract_allows_empty_completion() -> None:
    resp_ids, logprobs = tito_capture.extract_tokens(
        _resp(resp_ids=[], token_logprobs=[])
    )
    assert resp_ids == []
    assert logprobs == []


@pytest.mark.parametrize(
    "api_base",
    [
        "https://my-policy.modal.run/v1",
        "https://api.fireworks.ai/inference/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
    ],
)
def test_validate_endpoint_allows_controlled_hosts(api_base: str) -> None:
    tito_capture.validate_endpoint(api_base)  # must not raise


@pytest.mark.parametrize(
    "api_base",
    [
        "https://evil.com/v1",
        "http://169.254.169.254/latest/meta-data",  # metadata IP
        "https://modal.run.attacker.com/v1",  # suffix spoof
        "https://attacker.fireworks.ai/inference/v1",
        "http://my-policy.modal.run/v1",  # remote must be https
        "ftp://localhost/v1",
    ],
)
def test_validate_endpoint_rejects_everything_else(api_base: str) -> None:
    with pytest.raises(ValueError):
        tito_capture.validate_endpoint(api_base)


def test_validate_endpoint_enforces_configured_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERCOR_POLICY_ALLOWED_ORIGIN", "https://expected.modal.run")
    tito_capture.validate_endpoint("https://expected.modal.run/v1")
    with pytest.raises(ValueError, match="configured origin"):
        tito_capture.validate_endpoint("https://other.modal.run/v1")


def test_policy_model_name_strips_only_routing_prefixes() -> None:
    assert tito_capture._policy_model_name("fireworks_ai/qwen3-8b") == "qwen3-8b"
    assert tito_capture._policy_model_name("hosted_vllm/Qwen/Qwen3-8B") == (
        "Qwen/Qwen3-8B"
    )
    assert tito_capture._policy_model_name("openai/gpt-oss-20b") == (
        "openai/gpt-oss-20b"
    )
    assert tito_capture._policy_model_name("policy") == "policy"


def test_policy_model_name_strips_all_routing_prefixes() -> None:
    assert (
        tito_capture._policy_model_name("fireworks_ai/hosted_vllm/Qwen/Qwen3-8B")
        == "Qwen/Qwen3-8B"
    )


async def test_generate_tokens_feeds_ids_and_forces_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _resp(resp_ids=[7, 8], token_logprobs=[-0.1, -0.2])

    monkeypatch.setattr(tito_capture, "_request_completion", _fake)

    result = await tito_capture.generate_tokens(
        model="fireworks_ai/qwen3-8b",
        prompt_token_ids=[1, 2, 3],
        sampling_args={
            "temperature": 0.7,
            "max_tokens": 128,
            # Reserved keys the harness must not be able to override:
            "logprobs": 99,
            "return_token_ids": False,
            "prompt": [999],
            "timeout": 5,  # harness timeout must not collide with our explicit one
        },
        api_base="https://my-policy.modal.run/v1",
        api_key="secret",
        timeout=30,
    )

    assert result.completion_token_ids == [7, 8]
    assert result.token_logprobs == [-0.1, -0.2]
    assert captured["model"] == "qwen3-8b"
    assert captured["prompt_token_ids"] == [1, 2, 3]
    assert captured["api_base"] == "https://my-policy.modal.run/v1"
    assert captured["sampling_args"] == {"temperature": 0.7, "max_tokens": 128}
    assert captured["timeout"] == 30  # our explicit timeout, not the harness's 5


async def test_request_completion_sends_extensions_via_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    client_kwargs: dict[str, Any] = {}
    response = _resp(resp_ids=[2], token_logprobs=[-0.1])

    class RawResponse:
        def parse(self) -> Any:
            return response

    class WithRawResponse:
        async def create(self, **kwargs: Any) -> RawResponse:
            captured.update(kwargs)
            return RawResponse()

    class Completions:
        with_raw_response = WithRawResponse()

    class Client:
        completions = Completions()

    def _client(**kwargs: Any) -> Client:
        client_kwargs.update(kwargs)
        return Client()

    monkeypatch.setattr(tito_capture, "AsyncOpenAI", _client)
    await tito_capture._request_completion(
        model="Qwen/Qwen3-8B",
        prompt_token_ids=[1],
        sampling_args={"max_tokens": 8},
        api_base="https://policy.modal.run/v1",
        api_key="secret",
        timeout=30,
    )
    assert captured["prompt"] == [1]
    assert captured["extra_body"] == {"return_token_ids": True}
    assert captured["max_tokens"] == 8
    assert client_kwargs["http_client"].follow_redirects is False


async def test_generate_tokens_rejects_routing_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected(**_: Any) -> Any:
        raise AssertionError("transport must not run")

    monkeypatch.setattr(tito_capture, "_request_completion", _unexpected)
    with pytest.raises(ValueError, match="unsupported TITO sampling argument"):
        await tito_capture.generate_tokens(
            model="fireworks_ai/qwen3-8b",
            prompt_token_ids=[1],
            sampling_args={"base_url": "https://attacker.example/v1"},
            api_base="https://my-policy.modal.run/v1",
            api_key="secret",
            timeout=30,
        )


async def test_generate_tokens_validates_prompt_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected(**_: Any) -> Any:
        raise AssertionError("transport must not run")

    monkeypatch.setattr(tito_capture, "_request_completion", _unexpected)
    with pytest.raises(RuntimeError, match="prompt token IDs"):
        await tito_capture.generate_tokens(
            model="qwen",
            prompt_token_ids=[True],
            sampling_args={},
            api_base="https://policy.modal.run/v1",
            api_key="secret",
            timeout=30,
        )


def test_logit_bias_rejects_lossy_or_boolean_token_ids() -> None:
    with pytest.raises(ValueError, match="token IDs"):
        tito_capture._validated_sampling_args({"logit_bias": {True: 1}})
    with pytest.raises(ValueError, match="token IDs"):
        tito_capture._validated_sampling_args({"logit_bias": {1.2: 1}})


def test_extract_realistic_fireworks_completion_model() -> None:
    from openai.types.completion import Completion

    response = Completion.model_validate(
        {
            "id": "fw-1",
            "object": "text_completion",
            "created": 1,
            "model": "accounts/a/models/qwen",
            "prompt_token_ids": [1, 2],
            "choices": [
                {
                    "index": 0,
                    "text": "x",
                    "finish_reason": "stop",
                    "logprobs": {
                        "tokens": ["x"],
                        "token_logprobs": [-0.2],
                        "top_logprobs": [{}],
                        "text_offset": [0],
                        "token_ids": [7],
                    },
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
    )
    round_tripped = Completion.model_validate_json(json.dumps(response.model_dump()))
    result = tito_capture.extract_completion(round_tripped, prompt_token_ids=[1, 2])
    assert result.completion_token_ids == [7]
    assert result.token_logprobs == [-0.2]


def test_extract_reads_fireworks_logprobs_token_ids() -> None:
    choice = SimpleNamespace(
        provider_specific_fields=None,
        token_ids=None,
        finish_reason="length",
        text="x",
        logprobs=SimpleNamespace(token_ids=[7], token_logprobs=[-0.4]),
    )
    result = tito_capture.extract_completion(
        SimpleNamespace(choices=[choice], usage=None), prompt_token_ids=[1, 2]
    )
    assert result.completion_token_ids == [7]
    assert result.token_logprobs == [-0.4]
    assert result.finish_reason == "length"
