"""Flat TITO capture: env gate + per-rollout running token buffer.

Enabled only when ``MERCOR_TITO_CAPTURE=true`` — default-off so normal
trajectory runs are untouched. When active, the central Chat and Responses LLM
paths hand policy-model calls to :class:`TitoCaptureSession`, which feeds token
IDs to the policy endpoint, reconstructs the harness response, and grows one
flat sequence only while each turn is a provable append-only extension.

If a turn cannot be proven safe, the tape freezes with ``tito_complete=false``.
The model request is still served from a full render, but no further tokens are
appended to the flat training sequence.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from litellm import Choices
from litellm.files.main import ModelResponse
from litellm.types.utils import Message, Usage
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from runner.utils import tito_capture, tito_renderer

_active: contextvars.ContextVar[TitoCaptureSession | None] = contextvars.ContextVar(
    "tito_session", default=None
)

_RESPONSES_SEGMENT = "responses"


class _TapeMode(StrEnum):
    """Whether the flat token tape still accepts append-only extensions."""

    RECORDING = "recording"
    FROZEN = "frozen"


@dataclass(frozen=True)
class _TurnPlan:
    """Side-effect-free plan for one policy-model call."""

    renderer: Any
    prompt_ids: list[int]
    tape_delta: list[int]
    append_to_tape: bool
    freeze_reason: str | None
    messages: list[Any]
    tools: list[Any] | None
    tools_key: str
    template_key: str


@dataclass(frozen=True)
class _ParsedAssistant:
    content: str | None
    reasoning: str | None
    tool_calls: list[dict[str, Any]]
    parse_errors: list[str]


class TitoResponsesUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    output_tokens_details: dict[str, int] = Field(
        default_factory=lambda: {"reasoning_tokens": 0}
    )


class TitoResponsesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "response"
    model: str
    status: str
    output: list[dict[str, Any]]
    usage: TitoResponsesUsage
    incomplete_details: dict[str, str] | None = None


def get_active_session() -> TitoCaptureSession | None:
    return _active.get()


def set_active_session(
    session: TitoCaptureSession | None,
) -> contextvars.Token[TitoCaptureSession | None]:
    return _active.set(session)


def reset_active_session(
    token: contextvars.Token[TitoCaptureSession | None],
) -> None:
    _active.reset(token)


def nest_in_output(
    harness_output: dict[str, Any] | None, tito_payload: dict[str, Any]
) -> dict[str, Any]:
    """Merge capture under ``output["tito"]`` without clobbering harness keys."""
    return {**(harness_output or {}), "tito": tito_payload}


def _canonical_model(model: str) -> str:
    return "/".join(part for part in model.split("/") if part != _RESPONSES_SEGMENT)


def session_from_env(policy_model: str | None) -> TitoCaptureSession | None:
    """Build a capture session from the environment, or ``None`` if disabled."""
    if os.environ.get("MERCOR_TITO_CAPTURE", "").strip().lower() != "true":
        return None
    api_base = os.environ.get("MERCOR_POLICY_API_BASE", "").strip()
    if not api_base:
        raise RuntimeError(
            "MERCOR_TITO_CAPTURE is set but MERCOR_POLICY_API_BASE is missing."
        )
    if policy_model is None:
        raise RuntimeError("TITO capture needs a policy model but none was provided.")
    hf_id = tito_renderer.resolve_hf_id(policy_model)
    tito_renderer.validate_hf_id(hf_id)
    served_model = os.environ.get(
        "MERCOR_POLICY_SERVED_MODEL", ""
    ).strip() or _canonical_model(policy_model)
    explicit_key = os.environ.get("MERCOR_POLICY_API_KEY")
    api_key = explicit_key or (
        os.environ.get("FIREWORKS_API_KEY")
        if tito_capture.is_fireworks_endpoint(api_base)
        else None
    )
    tito_capture.validate_endpoint(api_base)
    default_max_tokens = int(os.environ.get("MERCOR_TITO_MAX_TOKENS", "8192"))
    if default_max_tokens <= 0:
        raise RuntimeError("MERCOR_TITO_MAX_TOKENS must be positive")
    return TitoCaptureSession(
        policy_model=policy_model,
        served_model=served_model,
        hf_id=hf_id,
        api_base=api_base,
        api_key=api_key or "dummy",
        default_max_tokens=default_max_tokens,
    )


def _as_payload(obj: Any) -> Any:
    """Serialize litellm/pydantic message + tool objects to JSON-safe values."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(exclude_none=True)
    return obj


def _json_key(value: Any, *, sort_keys: bool) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=sort_keys,
        default=str,
    )


def _tool_calls_to_dicts(
    tool_calls: list[tito_renderer.TitoParsedToolCall],
) -> list[dict[str, Any]] | None:
    if not tool_calls:
        return None
    out: list[dict[str, Any]] = []
    for i, call in enumerate(tool_calls):
        arguments = call.arguments
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {})
        out.append(
            {
                "id": call.id or f"tito_call_{i}",
                "type": "function",
                "function": {"name": call.name, "arguments": arguments},
            }
        )
    return out


def _normalize_responses_messages(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in messages:
        item = _as_payload(raw)
        if not isinstance(item, dict):
            raise ValueError("TITO Responses inputs must be dictionaries")
        role = item.get("role")
        item_type = item.get("type")
        if isinstance(role, str):
            normalized_role = "system" if role == "developer" else role
            message = {"role": normalized_role, "content": item.get("content")}
            for key in ("reasoning_content", "tool_calls", "tool_call_id", "name"):
                if item.get(key) is not None:
                    message[key] = list(item[key]) if key == "tool_calls" else item[key]
            out.append(message)
            continue
        if item_type == "reasoning":
            continue
        if item_type == "function_call":
            tool_call = {
                "id": item.get("call_id") or item.get("id"),
                "type": "function",
                "function": {
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "{}",
                },
            }
            if out and out[-1].get("role") == "assistant":
                out[-1].setdefault("tool_calls", []).append(tool_call)
            else:
                out.append({"role": "assistant", "tool_calls": [tool_call]})
            continue
        if item_type == "function_call_output":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": item.get("output") or "",
                }
            )
            continue
        raise ValueError(f"TITO does not support Responses input item {item_type!r}")
    return tito_renderer.normalize_messages(out)


def _normalize_responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ValueError(
                f"TITO does not support provider-hosted Responses tool {tool.get('type')!r}"
            )
        if isinstance(tool.get("function"), dict):
            normalized.append(dict(tool))
            continue
        function: dict[str, Any] = {"name": tool.get("name")}
        if tool.get("description") is not None:
            function["description"] = tool["description"]
        if tool.get("parameters") is not None:
            function["parameters"] = tool["parameters"]
        if tool.get("strict") is not None:
            function["strict"] = tool["strict"]
        normalized.append({"type": "function", "function": function})
    return normalized


class TitoCaptureSession:
    def __init__(
        self,
        *,
        policy_model: str,
        hf_id: str,
        api_base: str,
        api_key: str,
        default_max_tokens: int,
        served_model: str | None = None,
    ) -> None:
        self._policy_model = policy_model
        self._served_model = served_model or policy_model
        self._hf_id = hf_id
        self._api_base = api_base
        self._api_key = api_key
        self._default_max_tokens = default_max_tokens
        self._renderer: Any | None = None
        self._renderers: dict[str, tito_renderer.TitoRenderer] = {}
        self._token_ids: list[int] = []
        self._loss_mask: list[int] = []
        self._logprobs: list[float] = []
        self._prev_prompt_ids: list[int] | None = None
        self._prev_completion_ids: list[int] = []
        self._prev_messages: list[Any] | None = None
        self._expected_echo: Any = None
        self._prev_tools_key: str | None = None
        self._prev_template_key: str | None = None
        self._tape_mode = _TapeMode.RECORDING
        self._capture_complete = True
        self._error: str | None = None
        self._calls_seen = 0
        self._lock = asyncio.Lock()

    def handles(self, model: str) -> bool:
        """Capture only the policy model; auxiliary models take the normal path."""
        return _canonical_model(model) == _canonical_model(self._policy_model)

    def _split_request_args(
        self, extra_args: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        args = dict(extra_args)
        template = args.pop("chat_template_kwargs", None) or {}
        if not isinstance(template, dict):
            raise ValueError("TITO chat_template_kwargs must be a dictionary")
        template = dict(template)
        extra_body = args.pop("extra_body", None)
        if extra_body is not None:
            if not isinstance(extra_body, dict):
                raise ValueError("TITO extra_body must be a dictionary")
            nested = dict(extra_body)
            nested_template = nested.pop("chat_template_kwargs", None)
            if nested_template is not None:
                if template or not isinstance(nested_template, dict):
                    raise ValueError("TITO received conflicting chat_template_kwargs")
                template = dict(nested_template)
            reasoning = nested.pop("reasoning", None)
            if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
                args.setdefault("reasoning_effort", reasoning["effort"])
            if nested:
                raise ValueError(
                    f"unsupported TITO sampling argument: {sorted(nested)[0]}"
                )
        top_level_reasoning = args.pop("reasoning", None)
        if isinstance(top_level_reasoning, dict):
            args.setdefault("reasoning_effort", top_level_reasoning.get("effort"))
        elif top_level_reasoning is not None:
            raise ValueError("TITO reasoning must be a dictionary")
        reasoning_effort = args.pop("reasoning_effort", None)
        if reasoning_effort is not None:
            try:
                from renderers.base import MODEL_RENDERER_MAP  # noqa: PLC0415

                renderer_name = MODEL_RENDERER_MAP[self._hf_id]
            except (ImportError, KeyError):
                renderer_name = ""
            if renderer_name in {"gpt-oss", "hy3"}:
                template.setdefault("reasoning_effort", reasoning_effort)
        tool_choice = args.pop("tool_choice", None)
        if tool_choice not in (None, "auto"):
            raise ValueError("TITO supports only automatic tool choice")
        parallel = args.pop("parallel_tool_calls", None)
        if parallel not in (None, True):
            raise ValueError("TITO cannot enforce parallel_tool_calls=false")
        if args.pop("response_format", None) is not None:
            raise ValueError("TITO does not support response_format")
        for key in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
            if args.get(key) is None:
                args.pop(key, None)
        output_limits = [
            key
            for key in ("max_tokens", "max_output_tokens", "max_completion_tokens")
            if key in args
        ]
        if len(output_limits) > 1:
            raise ValueError("TITO accepts only one output-token limit")
        if output_limits and output_limits[0] != "max_tokens":
            args["max_tokens"] = args.pop(output_limits[0])
        elif not output_limits:
            args["max_tokens"] = self._default_max_tokens
        return template, args

    def _get_renderer(self, template: dict[str, Any]) -> Any:
        if self._renderer is not None:
            return self._renderer
        key = _json_key(template, sort_keys=True)
        renderer = self._renderers.get(key)
        if renderer is None:
            renderer = tito_renderer.TitoRenderer(self._hf_id, template)
            self._renderers[key] = renderer
        return renderer

    def _plan_turn(
        self,
        renderer: Any,
        messages: list[Any],
        tools: list[Any] | None,
        template_key: str,
    ) -> _TurnPlan:
        tools_key = _json_key(tools or [], sort_keys=False)
        if self._prev_prompt_ids is None or self._tape_mode == _TapeMode.FROZEN:
            prompt_ids = renderer.render_ids(messages, tools)
            return _TurnPlan(
                renderer=renderer,
                prompt_ids=prompt_ids,
                tape_delta=prompt_ids if not self._token_ids else [],
                append_to_tape=not self._token_ids
                and self._tape_mode == _TapeMode.RECORDING,
                freeze_reason=None,
                messages=messages,
                tools=tools,
                tools_key=tools_key,
                template_key=template_key,
            )

        reason: str | None = None
        observations: list[Any] = []
        previous = self._prev_messages or []
        if tools_key != self._prev_tools_key:
            reason = "tool definitions changed"
        elif template_key != self._prev_template_key:
            reason = "chat template configuration changed"
        elif messages[: len(previous)] != previous:
            reason = "message history was rewritten"
        else:
            tail = messages[len(previous) :]
            if not tail or tail[0] != self._expected_echo:
                reason = "previous assistant response was not echoed exactly"
            else:
                observations = tail[1:]
                if not observations:
                    reason = "message list did not add an observation"

        if reason is None:
            bridged = renderer.bridge(
                self._prev_prompt_ids,
                self._prev_completion_ids,
                observations,
                tools,
            )
            prefix = self._prev_prompt_ids + self._prev_completion_ids
            if bridged is None:
                reason = "renderer could not safely extend the turn"
            elif bridged[: len(prefix)] != prefix:
                reason = "renderer bridge did not preserve the sampled prefix"
            else:
                return _TurnPlan(
                    renderer=renderer,
                    prompt_ids=bridged,
                    tape_delta=bridged[len(prefix) :],
                    append_to_tape=True,
                    freeze_reason=None,
                    messages=messages,
                    tools=tools,
                    tools_key=tools_key,
                    template_key=template_key,
                )

        return _TurnPlan(
            renderer=renderer,
            prompt_ids=renderer.render_ids(messages, tools),
            tape_delta=[],
            append_to_tape=False,
            freeze_reason=reason,
            messages=messages,
            tools=tools,
            tools_key=tools_key,
            template_key=template_key,
        )

    async def _complete(
        self,
        messages: list[Any],
        tools: list[Any] | None,
        extra_args: dict[str, Any],
        timeout: float | int,
        *,
        surface: str,
    ) -> ModelResponse | TitoResponsesResponse:
        async with self._lock:
            template, sampling = self._split_request_args(extra_args)
            renderer = self._get_renderer(template)
            plan = self._plan_turn(
                renderer,
                messages,
                tools,
                _json_key(template, sort_keys=True),
            )
            completion = await tito_capture.generate_tokens(
                model=self._served_model,
                prompt_token_ids=plan.prompt_ids,
                sampling_args=sampling,
                api_base=self._api_base,
                api_key=self._api_key,
                timeout=timeout,
            )
            parsed = self._parse(renderer, completion.completion_token_ids, tools)
            assistant_payload = self._assistant_payload(
                parsed, include_reasoning=surface == "chat"
            )
            if surface == "responses":
                response: ModelResponse | TitoResponsesResponse = (
                    self._build_responses_response(parsed, completion)
                )
            else:
                response = self._build_chat_response(parsed, completion)
            self._commit(plan, completion, assistant_payload)
            if parsed.parse_errors:
                self._freeze_tape("renderer could not parse a tool call unambiguously")
            return response

    async def generate(
        self,
        model: str,
        messages: list[Any],
        tools: list[Any] | None,
        sampling_args: dict[str, Any],
        timeout: float | int,
    ) -> ModelResponse:
        del model
        payload_messages = [_as_payload(message) for message in messages]
        payload_tools = (
            [_as_payload(tool) for tool in tools] if tools is not None else None
        )
        response = await self._complete(
            payload_messages,
            payload_tools,
            sampling_args,
            timeout,
            surface="chat",
        )
        if not isinstance(response, ModelResponse):
            raise RuntimeError("TITO returned the wrong Chat response type")
        return response

    async def generate_responses(
        self,
        model: str,
        messages: list[Any],
        tools: list[dict[str, Any]],
        extra_args: dict[str, Any],
        timeout: float | int,
    ) -> TitoResponsesResponse:
        del model
        response = await self._complete(
            _normalize_responses_messages(messages),
            _normalize_responses_tools(tools),
            extra_args,
            timeout,
            surface="responses",
        )
        if not isinstance(response, TitoResponsesResponse):
            raise RuntimeError("TITO returned the wrong Responses response type")
        return response

    @staticmethod
    def _parse(
        renderer: Any, response_ids: list[int], tools: list[Any] | None
    ) -> _ParsedAssistant:
        parsed = renderer.parse(response_ids, tools)
        calls = _tool_calls_to_dicts(parsed.tool_calls) or []
        return _ParsedAssistant(
            parsed.content,
            parsed.reasoning_content,
            calls,
            parsed.parse_errors,
        )

    @staticmethod
    def _assistant_payload(
        parsed: _ParsedAssistant, *, include_reasoning: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": "assistant"}
        if parsed.content is not None:
            payload["content"] = parsed.content
        if include_reasoning and parsed.reasoning:
            payload["reasoning_content"] = parsed.reasoning
        if parsed.tool_calls:
            payload["tool_calls"] = parsed.tool_calls
        return payload

    def _build_chat_response(
        self,
        parsed: _ParsedAssistant,
        completion: tito_capture.TokenCompletion,
    ) -> ModelResponse:
        message = Message(
            role="assistant",
            content=parsed.content,
            tool_calls=parsed.tool_calls or None,
        )
        if parsed.reasoning:
            message.reasoning_content = parsed.reasoning  # type: ignore[attr-defined]
        finish_reason = (
            "tool_calls" if parsed.tool_calls else completion.finish_reason or "stop"
        )
        choice = Choices(finish_reason=finish_reason, index=0, message=message)
        usage = Usage(
            prompt_tokens=completion.usage["prompt_tokens"],
            completion_tokens=completion.usage["completion_tokens"],
            total_tokens=completion.usage["total_tokens"],
        )
        return ModelResponse(
            id=completion.request_id,
            model=completion.model or self._served_model,
            choices=[choice],
            usage=usage,
        )

    def _build_responses_response(
        self,
        parsed: _ParsedAssistant,
        completion: tito_capture.TokenCompletion,
    ) -> TitoResponsesResponse:
        output: list[dict[str, Any]] = []
        if parsed.reasoning:
            output.append(
                {
                    "type": "reasoning",
                    "status": "completed",
                    "content": [{"type": "reasoning_text", "text": parsed.reasoning}],
                    "summary": [{"type": "summary_text", "text": parsed.reasoning}],
                }
            )
        if parsed.content:
            output.append(
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": parsed.content,
                            "annotations": [],
                        }
                    ],
                }
            )
        for call in parsed.tool_calls:
            function = call["function"]
            output.append(
                {
                    "type": "function_call",
                    "status": "completed",
                    "id": call["id"],
                    "call_id": call["id"],
                    "name": function["name"],
                    "arguments": function["arguments"],
                }
            )
        finish = completion.finish_reason
        status = "incomplete" if finish in {"length", "content_filter"} else "completed"
        incomplete = None
        if finish == "length":
            incomplete = {"reason": "max_output_tokens"}
        elif finish == "content_filter":
            incomplete = {"reason": "content_filter"}
        usage = TitoResponsesUsage(
            input_tokens=completion.usage["prompt_tokens"],
            output_tokens=completion.usage["completion_tokens"],
            total_tokens=completion.usage["total_tokens"],
        )
        return TitoResponsesResponse(
            id=completion.request_id or "resp_tito",
            model=completion.model or self._served_model,
            status=status,
            output=output,
            usage=usage,
            incomplete_details=incomplete,
        )

    def _commit(
        self,
        plan: _TurnPlan,
        completion: tito_capture.TokenCompletion,
        assistant_payload: dict[str, Any],
    ) -> None:
        if plan.freeze_reason is not None:
            self._freeze_tape(plan.freeze_reason)
        elif plan.append_to_tape:
            self._append(plan.tape_delta, mask=0)
            self._append(
                completion.completion_token_ids,
                mask=1,
                logprobs=completion.token_logprobs,
            )
            self._prev_prompt_ids = plan.prompt_ids
            self._prev_completion_ids = completion.completion_token_ids
            self._prev_messages = plan.messages
            self._expected_echo = assistant_payload
            self._prev_tools_key = plan.tools_key
            self._prev_template_key = plan.template_key
        self._calls_seen += 1
        self._check_invariants()

    def _append(
        self, ids: list[int], *, mask: int, logprobs: list[float] | None = None
    ) -> None:
        if logprobs is not None and len(ids) != len(logprobs):
            raise RuntimeError("TITO append received misaligned token logprobs")
        self._token_ids.extend(ids)
        self._loss_mask.extend([mask] * len(ids))
        self._logprobs.extend([0.0] * len(ids) if logprobs is None else list(logprobs))

    def _check_invariants(self) -> None:
        if not (len(self._token_ids) == len(self._loss_mask) == len(self._logprobs)):
            raise RuntimeError("TITO tape arrays are misaligned")

    def _freeze_tape(self, reason: str) -> None:
        if self._tape_mode == _TapeMode.RECORDING:
            logger.warning(f"TITO flat capture abandoned: {reason}")
        self._tape_mode = _TapeMode.FROZEN
        self.mark_incomplete(reason)

    def mark_incomplete(self, reason: str) -> None:
        self._capture_complete = False
        if self._error is None:
            self._error = reason or "capture incomplete"

    def output_dict(self) -> dict[str, Any]:
        """Serialize the flat sequence for ``AgentTrajectoryOutput.output["tito"]``."""
        self._check_invariants()
        complete = self._capture_complete and self._calls_seen > 0
        error = self._error
        if self._calls_seen == 0 and error is None:
            error = "no matching central policy call was captured"
        out: dict[str, Any] = {
            "tito_version": 1,
            "tito_complete": complete,
            "hf_model_id": self._hf_id,
            "token_ids": self._token_ids,
            "loss_mask": self._loss_mask,
            "logprobs": self._logprobs,
        }
        if error is not None:
            out["tito_error"] = error
        return out
