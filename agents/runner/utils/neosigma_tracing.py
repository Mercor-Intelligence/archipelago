"""NeoSigma tracing for the shared runner and its model and tool boundaries."""

import asyncio
import base64
import inspect
import json
import os
import time
from contextlib import nullcontext
from contextvars import ContextVar
from functools import cache, wraps
from io import BytesIO
from typing import Any

import neosigma
from litellm.experimental_mcp_client import call_openai_tool as _call_openai_tool
from loguru import logger
from neosigma.spans import end_chat, end_tool, start_chat, start_tool
from neosigma.types import (
    ChatEnd,
    ChatStart,
    PromptMessage,
    TokenUsage,
    ToolEnd,
    ToolStart,
)
from opentelemetry.trace import get_current_span, use_span

from runner.utils.neosigma_artifacts import Direction, WorkspaceArtifacts, upload_parts

_active = ContextVar("neosigma_trace_active", default=False)
_workspace: ContextVar[WorkspaceArtifacts | None] = ContextVar(
    "neosigma_workspace", default=None
)
_secrets = {
    "authorization",
    "api_key",
    "api_token",
    "access_token",
    "x_api_key",
    "x_neosigma_api_key",
}


def _flag(name: str, default: str = "") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def _artifacts_enabled() -> bool:
    return bool(os.getenv("NEOSIGMA_API_KEY")) and _flag(
        "NEOSIGMA_CAPTURE_CONTENT", "true"
    )


@cache
def _init() -> None:
    neosigma.init()


def _recordable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        return {
            key: _recordable(item)
            for key, item in value.items()
            if str(key).lower().replace("-", "_") not in _secrets
        }
    if isinstance(value, (list, tuple)):
        return [_recordable(item) for item in value]
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _json(value: Any) -> str:
    try:
        return json.dumps(_recordable(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError) as exc:
        return json.dumps({"serialization_error": type(exc).__name__})


async def _attach_payload(
    text: str, name: str, direction: Direction, *, include_small: bool = False
) -> None:
    if not _artifacts_enabled():
        return
    try:
        max_chars = int(os.getenv("NEOSIGMA_MAX_CONTENT_CHARS", "24000"))
        if include_small or (max_chars and len(text) > min(max_chars, 12000)):
            await asyncio.to_thread(
                upload_parts, BytesIO(text.encode("utf-8")), name, direction
            )
    except Exception as exc:
        get_current_span().set_attribute(
            "archipelago.artifacts.payload_error", type(exc).__name__
        )
        logger.warning(
            "NeoSigma payload artifact capture failed ({})", type(exc).__name__
        )


async def capture_staged_inputs() -> None:
    if workspace := _workspace.get():
        await workspace.drain()
        await workspace.capture("staged-inputs.zip", "input", starting=True)


def trace_agent_run(run):
    signature = inspect.signature(run)

    @wraps(run)
    async def wrapped(*args, **kwargs):
        if not _flag("NEOSIGMA_ENABLED", "true") or not (
            os.getenv("NEOSIGMA_API_KEY") or _flag("NEOSIGMA_CONSOLE_EXPORT")
        ):
            return await run(*args, **kwargs)

        from runner.utils.file_staging import sandbox_url_from_gateway
        from runner.utils.logging.final_answer import peek_final_answer
        from runner.utils.logging.terminal_error import peek_terminal_error

        fields = signature.bind(*args, **kwargs).arguments
        trajectory_id = fields["trajectory_id"]
        messages = fields["initial_messages"]
        user_message = next(
            (
                message.get("content")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        workspace = None
        if _artifacts_enabled() and fields["mcp_gateway_url"]:
            workspace = WorkspaceArtifacts(
                sandbox_url_from_gateway(fields["mcp_gateway_url"]),
                fields["mcp_gateway_auth_token"],
            )

        _init()
        active_token = _active.set(True)
        workspace_token = _workspace.set(workspace)
        try:
            with neosigma.turn(
                session_id=trajectory_id,
                turn_id=trajectory_id,
                user_message=user_message
                if isinstance(user_message, str)
                else _json(user_message),
                agent_id=fields.get("agent_id")
                or fields["agent_config"].agent_config_id,
                source="archipelago",
                source_ref=trajectory_id,
            ) as turn:
                turn.set_attributes({"archipelago.model": fields["orchestrator_model"]})
                await _attach_payload(
                    _json(messages),
                    "initial-messages.json",
                    "input",
                    include_small=True,
                )
                if workspace:
                    await workspace.capture(
                        "starting-files.zip", "input", starting=True
                    )
                try:
                    result = await run(*args, **kwargs)
                    answer = result.final_answer or peek_final_answer(trajectory_id)
                    if answer is None and result.messages:
                        last = _recordable(result.messages[-1])
                        if isinstance(last, dict) and last.get("role") == "assistant":
                            answer = last.get("content")
                    if answer is not None:
                        turn.set_content(
                            completion=answer
                            if isinstance(answer, str)
                            else _json(answer)
                        )
                    attributes = {"archipelago.status": str(result.status)}
                    if error := result.error or peek_terminal_error(trajectory_id):
                        attributes["archipelago.error"] = error
                    turn.set_attributes(attributes)
                    await _attach_payload(
                        _json(result),
                        "trajectory-result.json",
                        "output",
                        include_small=True,
                    )
                    return result
                finally:
                    if workspace:
                        await workspace.drain()
                        await workspace.capture("final-files.zip", "output", final=True)
        finally:
            _workspace.reset(workspace_token)
            _active.reset(active_token)
            await asyncio.to_thread(neosigma.flush)

    return wrapped


def trace_model(call):
    @wraps(call)
    async def wrapped(model, messages, tools, *args, **kwargs):
        if not _active.get():
            return await call(model, messages, tools, *args, **kwargs)

        prompt = [
            PromptMessage(role=str(message.get("role", "")), content=_json(message))
            for message in _recordable(messages)
        ]
        span = start_chat(ChatStart(model=model, prompt_messages=prompt))
        result = ChatEnd()
        try:
            with use_span(
                span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                await _attach_payload(
                    _json({"messages": messages, "tools": tools}),
                    "model-input.json",
                    "input",
                    include_small=bool(tools),
                )
                response = await call(model, messages, tools, *args, **kwargs)
                completion = _json(response)
                await _attach_payload(completion, "model-output.json", "output")
                usage = _recordable(getattr(response, "usage", None)) or {}
                result = ChatEnd(
                    completion=completion,
                    usage=TokenUsage(
                        input_tokens=max(
                            0,
                            usage.get("input_tokens")
                            or usage.get("prompt_tokens")
                            or 0,
                        ),
                        output_tokens=max(
                            0,
                            usage.get("output_tokens")
                            or usage.get("completion_tokens")
                            or 0,
                        ),
                    ),
                )
                return response
        except BaseException as exc:
            result = ChatEnd(is_error=True, error_message=type(exc).__name__)
            raise
        finally:
            end_chat(span, result)

    return wrapped


async def _finish_tool_files(workspace, span, outcome: ToolEnd, end_time: int) -> None:
    try:
        if workspace.needs_baseline:
            span.set_attribute("archipelago.artifacts.unattributed", True)
            await workspace.capture("workspace-after-tool.zip", "output")
        else:
            await workspace.capture("tool-files.zip", "output")
    finally:
        end_tool(span, outcome, end_time=end_time)


def _tool_failed(result: Any) -> bool:
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        return True
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    return isinstance(structured, dict) and structured.get("success") is False


async def trace_tool(name, arguments, invoke):
    if not _active.get():
        return await invoke()
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    payload = _json(arguments)
    span = start_tool(ToolStart(name=name, args_json=payload))
    outcome = ToolEnd()
    workspace = _workspace.get()
    deferred = False
    try:
        with use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            async with workspace.lock if workspace else nullcontext():
                if workspace:
                    await workspace.drain()
                    if workspace.needs_baseline:
                        await workspace.capture("workspace-before-tool.zip", "input")
                    workspace.attach_referenced_inputs(arguments)
                await _attach_payload(payload, "tool-input.json", "input")
                try:
                    result = await invoke()
                    content = _json(result)
                    await _attach_payload(content, "tool-output.json", "output")
                    outcome = ToolEnd(
                        result=content,
                        is_error=_tool_failed(result),
                    )
                    return result
                except BaseException as exc:
                    outcome = ToolEnd(is_error=True, error_message=type(exc).__name__)
                    raise
                finally:
                    if workspace:
                        # Keep snapshot I/O outside the completed tool's timeout.
                        workspace.pending = asyncio.create_task(
                            _finish_tool_files(workspace, span, outcome, time.time_ns())
                        )
                        deferred = True
    except BaseException as exc:
        outcome = ToolEnd(is_error=True, error_message=type(exc).__name__)
        raise
    finally:
        if not deferred:
            end_tool(span, outcome)


async def call_openai_tool(session, tool_call, *args, **kwargs):
    if not _active.get():
        return await _call_openai_tool(session, tool_call, *args, **kwargs)
    function = (
        tool_call["function"] if isinstance(tool_call, dict) else tool_call.function
    )
    name = function["name"] if isinstance(function, dict) else function.name
    arguments = (
        function.get("arguments") if isinstance(function, dict) else function.arguments
    )
    return await trace_tool(
        name,
        arguments,
        lambda: _call_openai_tool(session, tool_call, *args, **kwargs),
    )
