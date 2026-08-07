"""Token-in / token-out transport for flat TITO capture.

The buffer session (see ``tito_session``) owns the running token sequence and
feeds *token ids* — never messages — to the self-served policy endpoint, so the
tokens we record are byte-for-byte what the model conditioned on and sampled.
This module is just the wire: call ``/completions`` with a token-id prompt and
``return_token_ids`` + ``logprobs``, then normalize the provider response,
failing loud if the backend isn't capture-capable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

# Only the self-served policy backends we control may receive a capture call.
# A bare/metadata/loopback IP never matches these suffixes, so it's rejected.
_ALLOWED_HOST_SUFFIXES = (".modal.run", ".fireworks.ai")
_ALLOWED_LOCALHOSTS = ("localhost", "127.0.0.1")

# Keys the harness's sampling args may safely control on both OpenAI-compatible
# vLLM and Fireworks text-completion endpoints.
_ALLOWED_SAMPLING_KEYS = frozenset(
    {
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
    }
)
_CAPTURE_OWNED_KEYS = frozenset(
    {
        "model",
        "prompt",
        "logprobs",
        "top_logprobs",
        "return_token_ids",
        "stream",
        "num_retries",
        "max_retries",
        "timeout",
        "n",
        "echo",
        "best_of",
    }
)
_ROUTING_KEYS = frozenset(
    {
        "api_base",
        "base_url",
        "api_key",
        "api_version",
        "custom_llm_provider",
        "model_list",
        "fallbacks",
        "client",
        "shared_session",
        "extra_headers",
        "extra_body",
    }
)

_KNOWN_PROVIDER_PREFIXES = (
    "responses/",
    "fireworks_ai/",
    "hosted_vllm/",
    "vllm/",
)


@dataclass(frozen=True)
class TokenCompletion:
    prompt_token_ids: list[int]
    completion_token_ids: list[int]
    token_logprobs: list[float]
    text: str
    finish_reason: str | None
    native_finish_reason: str | None
    usage: dict[str, int]
    request_id: str | None
    model: str | None


def validate_endpoint(api_base: str) -> None:
    """Reject any endpoint that is not a self-served policy backend (SSRF guard).

    Capture forwards the policy api_key and forces logprobs/return_token_ids, so
    it must only ever reach configured policy hosts. Plain http is allowed for
    localhost only; everything remote must be https.
    """
    parsed = urlparse(api_base)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"policy api_base must be http(s): {api_base!r}")
    host = (parsed.hostname or "").lower()
    is_localhost = host in _ALLOWED_LOCALHOSTS
    if not (is_localhost or host.endswith(_ALLOWED_HOST_SUFFIXES)):
        raise ValueError(f"policy api_base host not allowed: {host!r}")
    if parsed.scheme == "http" and not is_localhost:
        raise ValueError(f"remote policy api_base must use https: {api_base!r}")


def is_fireworks_endpoint(api_base: str) -> bool:
    return (urlparse(api_base).hostname or "").lower().endswith(".fireworks.ai")


def _policy_model_name(model: str) -> str:
    """Strip outer routing prefixes while preserving model namespaces."""
    name = model
    changed = True
    while changed:
        changed = False
        for prefix in _KNOWN_PROVIDER_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                changed = True
                break
    return name


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"TITO sampling argument {name!r} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"TITO sampling argument {name!r} must be finite")
    return result


def _validated_sampling_args(sampling_args: dict[str, Any]) -> dict[str, Any]:
    forbidden = sorted(set(sampling_args).intersection(_ROUTING_KEYS))
    if forbidden:
        raise ValueError(f"unsupported TITO sampling argument: {forbidden[0]}")
    unknown = sorted(
        set(sampling_args).difference(_ALLOWED_SAMPLING_KEYS, _CAPTURE_OWNED_KEYS)
    )
    if unknown:
        raise ValueError(f"unsupported TITO sampling argument: {unknown[0]}")

    values = {
        key: value
        for key, value in sampling_args.items()
        if key in _ALLOWED_SAMPLING_KEYS and value is not None
    }
    aliases = [
        key
        for key in ("max_tokens", "max_output_tokens", "max_completion_tokens")
        if key in values
    ]
    if len(aliases) > 1:
        raise ValueError("TITO accepts only one output-token limit")
    if aliases:
        raw_max = values.pop(aliases[0])
        if isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max <= 0:
            raise ValueError("TITO max_tokens must be a positive integer")
        values["max_tokens"] = raw_max
    if "temperature" in values and _number(values["temperature"], "temperature") < 0:
        raise ValueError("TITO temperature must be nonnegative")
    if "top_p" in values:
        top_p = _number(values["top_p"], "top_p")
        if not 0 <= top_p <= 1:
            raise ValueError("TITO top_p must be between 0 and 1")
    if "seed" in values and (
        isinstance(values["seed"], bool) or not isinstance(values["seed"], int)
    ):
        raise ValueError("TITO seed must be an integer")
    for name in ("frequency_penalty", "presence_penalty"):
        if name in values:
            penalty = _number(values[name], name)
            if not -2 <= penalty <= 2:
                raise ValueError(f"TITO {name} must be between -2 and 2")
    if "logit_bias" in values:
        bias = values["logit_bias"]
        if not isinstance(bias, dict):
            raise ValueError("TITO logit_bias must be a dictionary")
        normalized_bias: dict[str, int] = {}
        for token, value in bias.items():
            if isinstance(token, bool):
                raise ValueError("TITO logit_bias keys must be token IDs")
            if isinstance(token, int):
                token_id = token
            elif isinstance(token, str) and token.isdecimal():
                token_id = int(token)
            else:
                raise ValueError("TITO logit_bias keys must be token IDs")
            if (
                token_id < 0
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not -100 <= value <= 100
            ):
                raise ValueError("TITO logit_bias contains an invalid entry")
            normalized_bias[str(token_id)] = value
        values["logit_bias"] = normalized_bias
    return values


async def _request_completion(
    *,
    model: str,
    prompt_token_ids: list[int],
    sampling_args: dict[str, Any],
    api_base: str,
    api_key: str,
    timeout: float | int,
) -> Any:
    async with httpx.AsyncClient(follow_redirects=False) as http_client:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            http_client=http_client,
            max_retries=0,
            timeout=timeout,
        )
        response = await client.completions.with_raw_response.create(
            model=model,
            prompt=prompt_token_ids,
            logprobs=1,
            n=1,
            echo=False,
            extra_body={"return_token_ids": True},
            **sampling_args,
        )
        return response.parse()


async def generate_tokens(
    *,
    model: str,
    prompt_token_ids: list[int],
    sampling_args: dict[str, Any],
    api_base: str,
    api_key: str,
    timeout: float | int,
) -> TokenCompletion:
    """Feed token ids to the policy ``/completions`` endpoint."""
    validate_endpoint(api_base)
    if not prompt_token_ids:
        raise ValueError("TITO prompt token IDs must be nonempty")
    fwd = _validated_sampling_args(sampling_args)
    validated_prompt = _validated_ids(prompt_token_ids, "prompt token IDs")
    resp = await _request_completion(
        model=_policy_model_name(model),
        prompt_token_ids=validated_prompt,
        sampling_args=fwd,
        api_base=api_base,
        api_key=api_key,
        timeout=timeout,
    )
    return extract_completion(resp, prompt_token_ids=validated_prompt)


def _mapping_value(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _token_ids_from_choice(choice: Any) -> list[int] | None:
    """Sampled token ids from a /completions choice across provider versions."""
    ids = _mapping_value(choice, "token_ids")
    psf = _mapping_value(choice, "provider_specific_fields") or {}
    if ids is None and isinstance(psf, dict):
        ids = psf.get("token_ids")
    logprobs = _mapping_value(choice, "logprobs")
    if ids is None and logprobs is not None:
        ids = _mapping_value(logprobs, "token_ids")
    return list(ids) if ids is not None else None


def _validated_ids(values: list[Any], name: str) -> list[int]:
    ids: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"TITO {name} contain an invalid token ID")
        ids.append(value)
    return ids


def _usage_dict(prompt_count: int, completion_count: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt_count,
        "completion_tokens": completion_count,
        "total_tokens": prompt_count + completion_count,
    }


def extract_completion(resp: Any, *, prompt_token_ids: list[int]) -> TokenCompletion:
    """Normalize one token-in completion response and enforce tape invariants."""
    choices = _mapping_value(resp, "choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise RuntimeError("TITO capture requires exactly one completion choice")
    choice = choices[0]
    raw_ids = _token_ids_from_choice(choice)
    if raw_ids is None:
        raise RuntimeError(
            "TITO capture requires a capture-capable backend that returns token "
            "ids (vLLM / Fireworks `return_token_ids`); got none."
        )
    response_ids = _validated_ids(raw_ids, "completion token IDs")
    returned_prompt = _mapping_value(resp, "prompt_token_ids")
    if returned_prompt is not None:
        validated_prompt = _validated_ids(
            list(returned_prompt), "returned prompt token IDs"
        )
        if validated_prompt != prompt_token_ids:
            raise RuntimeError("TITO backend changed the supplied prompt token IDs")

    lp = _mapping_value(choice, "logprobs")
    raw_logprobs = _mapping_value(lp, "token_logprobs") if lp is not None else None
    logprobs = list(raw_logprobs) if raw_logprobs is not None else []
    if len(logprobs) != len(response_ids):
        raise RuntimeError(
            f"TITO logprobs unusable: {len(logprobs)} logprobs vs "
            f"{len(response_ids)} response tokens."
        )
    normalized_logprobs: list[float] = []
    for value in logprobs:
        if value is None:
            raise RuntimeError("TITO logprobs contain null values.")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RuntimeError("TITO logprobs contain non-numeric values.")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise RuntimeError("TITO logprobs contain non-finite values.")
        normalized_logprobs.append(normalized)

    finish = _mapping_value(choice, "finish_reason")
    finish_reason = str(finish) if finish is not None else None
    psf = _mapping_value(choice, "provider_specific_fields")
    native_finish = psf.get("native_finish_reason") if isinstance(psf, dict) else None
    text = _mapping_value(choice, "text")
    usage = _usage_dict(len(prompt_token_ids), len(response_ids))
    return TokenCompletion(
        prompt_token_ids=list(prompt_token_ids),
        completion_token_ids=response_ids,
        token_logprobs=normalized_logprobs,
        text=text if isinstance(text, str) else "",
        finish_reason=finish_reason,
        native_finish_reason=(
            str(native_finish) if native_finish is not None else finish_reason
        ),
        usage=usage,
        request_id=_mapping_value(resp, "id"),
        model=_mapping_value(resp, "model"),
    )


def extract_tokens(resp: Any) -> tuple[list[int], list[float]]:
    """Backward-compatible extraction helper for focused unit tests."""
    result = extract_completion(resp, prompt_token_ids=[])
    return result.completion_token_ids, result.token_logprobs
