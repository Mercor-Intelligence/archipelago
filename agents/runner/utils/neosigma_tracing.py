"""Optional tracing for the shared runner and its model and MCP boundaries."""

import asyncio
import base64
import inspect
import json
import math
import os
import sys
from contextlib import ExitStack
from contextvars import ContextVar
from functools import cache, wraps
from io import BytesIO
from tempfile import TemporaryFile
from typing import Any, BinaryIO

from litellm.experimental_mcp_client import call_openai_tool as _call_openai_tool
from loguru import logger
from opentelemetry.trace import use_span

from runner.utils.neosigma_artifacts import (
    SnapshotUploadError,
    publish_artifact,
    read_workspace,
)

_active = ContextVar("neosigma_trace_active", default=False)
_staging = ContextVar[tuple[ExitStack, list[BinaryIO]] | None](
    "neosigma_staging", default=None
)
_CAPTURE_TIMEOUT_SECONDS = 30.0
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


def _capture_timeout() -> float:
    value = os.getenv("NEOSIGMA_CAPTURE_TIMEOUT_SECONDS")
    if value is not None:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds > 0:
                return seconds
        except ValueError:
            pass
        logger.warning(
            "Invalid NeoSigma capture timeout; using {} seconds",
            _CAPTURE_TIMEOUT_SECONDS,
        )
    return _CAPTURE_TIMEOUT_SECONDS


def _observe(operation, call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        logger.warning("NeoSigma {} failed ({})", operation, type(exc).__name__)
        return None


@cache
def _init():
    import neosigma

    neosigma.init()
    return neosigma


async def flush_traces() -> None:
    """Flush batch exports after the CLI has persisted the trajectory result."""
    try:
        if _init.cache_info().currsize:
            await asyncio.to_thread(_init().flush)
    except Exception as exc:
        logger.warning("NeoSigma flush failed ({})", type(exc).__name__)


def record_staged_inputs(archive: bytes) -> None:
    """Record the exact archive successfully staged by the runner as task input."""
    if state := _staging.get():
        files, inputs = state

        def record():
            file = files.enter_context(TemporaryFile())
            file.write(archive)
            file.seek(0)
            inputs.append(file)

        _observe("staged input capture", record)


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
    return json.dumps(_recordable(value), ensure_ascii=True, default=str)


def _capture_error(turn, name: str, exc: Exception) -> None:
    detail = str(exc) if isinstance(exc, SnapshotUploadError) else type(exc).__name__
    logger.warning("NeoSigma capture failed for {} ({})", name, detail)
    _observe(
        "capture diagnostics",
        turn.set_attributes,
        {f"archipelago.artifacts.{name}.error": detail},
    )


async def _collect_workspace(turn, name, gateway, token):
    if not _artifacts_enabled() or not gateway:
        return None
    try:
        from runner.utils.file_staging import sandbox_url_from_gateway

        return await read_workspace(sandbox_url_from_gateway(gateway), token)
    except Exception as exc:
        _capture_error(turn, name, exc)
        return None


async def _attach(sdk, turn, content, name, direction, media_type):
    if content is None:
        return
    try:
        with content:
            location, display_name = await publish_artifact(content, name, media_type)
        sdk.artifact(location, name=display_name, direction=direction)
    except Exception as exc:
        _capture_error(turn, name, exc)


async def _publish_evidence(sdk, turn, starting, final, staged, messages, result):
    for index, archive in enumerate(staged, 1):
        await _attach(
            sdk,
            turn,
            archive,
            f"staged-inputs-{index:03d}.tar.gz",
            "input",
            "application/gzip",
        )
    await _attach(
        sdk, turn, starting, "starting-workspace.tar.gz", "input", "application/gzip"
    )
    await _attach(
        sdk, turn, final, "final-workspace.tar.gz", "output", "application/gzip"
    )
    initial = await asyncio.to_thread(
        _observe, "initial message encoding", _json, messages
    )
    if initial is not None:
        await _attach(
            sdk,
            turn,
            BytesIO(initial.encode()),
            "initial-messages.json",
            "input",
            "application/json",
        )
    if result is not None:
        transcript = await asyncio.to_thread(
            _observe, "trajectory encoding", _json, result
        )
        if transcript is not None:
            await _attach(
                sdk,
                turn,
                BytesIO(transcript.encode()),
                "trajectory-result.json",
                "output",
                "application/json",
            )


def _finish_result(turn, result, trajectory_id: str) -> None:
    from runner.utils.logging.final_answer import peek_final_answer
    from runner.utils.logging.terminal_error import peek_terminal_error

    answer = result.final_answer or peek_final_answer(trajectory_id)
    if answer is None and result.messages:
        last = _recordable(result.messages[-1])
        if isinstance(last, dict) and last.get("role") == "assistant":
            answer = last.get("content")
    if answer is not None:
        turn.set_content(
            completion=answer if isinstance(answer, str) else _json(answer)
        )
    attributes = {"archipelago.status": str(result.status)}
    if error := result.error or peek_terminal_error(trajectory_id):
        attributes["archipelago.error"] = error
    turn.set_attributes(attributes)


def _end_if_open(span) -> None:
    if span.is_recording():
        span.end()


def trace_agent_run(run):
    signature = inspect.signature(run)

    @wraps(run)
    async def wrapped(*args, **kwargs):
        if not _flag("NEOSIGMA_ENABLED", "true") or not (
            os.getenv("NEOSIGMA_API_KEY") or _flag("NEOSIGMA_CONSOLE_EXPORT")
        ):
            return await run(*args, **kwargs)

        scope = ExitStack()
        try:
            fields = signature.bind(*args, **kwargs).arguments
            sdk = _init()
            trajectory_id = fields["trajectory_id"]
            messages = fields["initial_messages"]
            user_message = next(
                (
                    msg.get("content")
                    for msg in reversed(messages)
                    if msg.get("role") == "user"
                ),
                "",
            )
            turn = scope.enter_context(
                sdk.turn(
                    session_id=trajectory_id,
                    turn_id=trajectory_id,
                    user_message=user_message
                    if isinstance(user_message, str)
                    else _json(user_message),
                    agent_id=fields.get("agent_id")
                    or fields["agent_config"].agent_config_id,
                    source="archipelago",
                    source_ref=trajectory_id,
                )
            )
            gateway = fields["mcp_gateway_url"]
            auth_token = fields["mcp_gateway_auth_token"]
        except Exception as exc:
            logger.warning("NeoSigma turn setup failed ({})", type(exc).__name__)
            _observe("turn cleanup", scope.close)
            return await run(*args, **kwargs)

        active = _active.set(True)
        files = ExitStack()
        capture = _artifacts_enabled()
        timeout = _capture_timeout() if capture else _CAPTURE_TIMEOUT_SECONDS
        staged: list[BinaryIO] = []
        staging = _staging.set((files, staged) if capture else None)
        starting = None
        result = None
        try:
            _observe(
                "turn metadata",
                turn.set_attributes,
                {"archipelago.model": fields["orchestrator_model"]},
            )
            if capture:
                try:
                    async with asyncio.timeout(timeout):
                        starting = await _collect_workspace(
                            turn, "starting", gateway, auth_token
                        )
                    if starting is not None:
                        files.enter_context(starting)
                except TimeoutError as exc:
                    _capture_error(turn, "starting", exc)
            try:
                result = await run(*args, **kwargs)
                _observe("turn outcome", _finish_result, turn, result, trajectory_id)
                return result
            finally:
                if capture:
                    try:
                        async with asyncio.timeout(timeout):
                            final = await _collect_workspace(
                                turn, "final", gateway, auth_token
                            )
                            if final is not None:
                                files.enter_context(final)
                            await _publish_evidence(
                                sdk, turn, starting, final, staged, messages, result
                            )
                    except TimeoutError as exc:
                        _capture_error(turn, "after-run", exc)
        finally:
            _observe("snapshot cleanup", files.close)
            _staging.reset(staging)
            _active.reset(active)
            error = sys.exc_info()
            if error[1] is None and getattr(result, "status", None) in {
                "failed",
                "error",
                "cancelled",
            }:
                failure = RuntimeError(
                    f"Archipelago trajectory returned {result.status}"
                )
                error = type(failure), failure, None
            _observe("turn completion", scope.__exit__, *error)

    return wrapped


async def _trace_call(invoke, start, finish):
    """Observe a call while keeping SDK failures separate from its result."""
    span = _observe("span setup", start)
    if span is None:
        return await invoke()
    scope = ExitStack()
    _observe(
        "span activation",
        lambda: scope.enter_context(
            use_span(
                span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            )
        ),
    )
    result = None
    error = None
    try:
        result = await invoke()
        return result
    except BaseException as exc:
        error = exc
        raise
    finally:
        _observe("span completion", finish, span, result, error)
        _observe("span end", _end_if_open, span)
        _observe("span cleanup", scope.close)


def trace_model(call):
    @wraps(call)
    async def wrapped(model, messages, tools, *args, **kwargs):
        if not _active.get():
            return await call(model, messages, tools, *args, **kwargs)

        def start():
            from neosigma.spans import start_chat
            from neosigma.types import ChatStart, PromptMessage

            prompt = [
                PromptMessage(role=str(msg.get("role", "")), content=_json(msg))
                for msg in _recordable(messages)
            ]
            schemas = _json(tools) if tools else None
            span = start_chat(ChatStart(model=model, prompt_messages=prompt))
            if schemas is not None:
                _observe(
                    "tool schemas",
                    span.set_attribute,
                    "archipelago.tool_schemas",
                    schemas,
                )
            return span

        def finish(span, response, error):
            from neosigma.spans import end_chat
            from neosigma.types import ChatEnd, TokenUsage

            if error is not None:
                end_chat(
                    span, ChatEnd(is_error=True, error_message=type(error).__name__)
                )
                return
            usage = _recordable(getattr(response, "usage", None)) or {}
            end_chat(
                span,
                ChatEnd(
                    completion=_json(response),
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
                ),
            )

        return await _trace_call(
            lambda: call(model, messages, tools, *args, **kwargs), start, finish
        )

    return wrapped


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

    def start():
        from neosigma.spans import start_tool
        from neosigma.types import ToolStart

        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        return start_tool(ToolStart(name=name, args_json=_json(args)))

    def finish(span, result, error):
        from neosigma.spans import end_tool
        from neosigma.types import ToolEnd

        end_tool(
            span,
            ToolEnd(
                result=_json(result) if error is None else None,
                is_error=error is not None or _tool_failed(result),
                error_message=type(error).__name__ if error is not None else "",
            ),
        )

    return await _trace_call(invoke, start, finish)


async def call_openai_tool(session, tool_call, *args, **kwargs):
    async def invoke():
        return await _call_openai_tool(session, tool_call, *args, **kwargs)

    if not _active.get():
        return await invoke()
    try:
        function = (
            tool_call["function"] if isinstance(tool_call, dict) else tool_call.function
        )
        name = function["name"] if isinstance(function, dict) else function.name
        arguments = (
            function.get("arguments")
            if isinstance(function, dict)
            else function.arguments
        )
    except Exception as exc:
        logger.warning("NeoSigma tool metadata failed ({})", type(exc).__name__)
        return await invoke()
    return await trace_tool(name, arguments, invoke)
