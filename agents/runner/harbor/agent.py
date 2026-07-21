"""Archipelago agent adapter used by Studio Harbor batch trajectories."""

from __future__ import annotations

import json
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from loguru import logger

from modal_helpers import fetch_agent_config
from runner.utils.decorators import (
    actor_email_ctx,
    actor_ip_ctx,
    campaign_id_ctx,
    impersonation_enabled_ctx,
    trajectory_batch_id_ctx,
)
from runner.utils.logging.main import setup_logger, teardown_logger
from runner.utils.settings import (
    gateway_routing_enabled_ctx,
    priority_ctx,
    work_unit_ctx,
)


def _is_harbor_package_missing(exc: ModuleNotFoundError) -> bool:
    return exc.name == "harbor"


try:
    from harbor.agents.base import BaseAgent  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError as exc:
    # Unit-test import; the dedicated Modal image installs Harbor. Do not hide a
    # missing Harbor submodule or transitive dependency when Harbor is present.
    if not _is_harbor_package_missing(exc):
        raise
    BaseAgent = object  # type: ignore[assignment,misc]


async def run_archipelago_agent(**kwargs: Any) -> Any:
    """Import the full agent registry only inside the dedicated runtime image."""

    from runner.main import main

    return await main(**kwargs)


def _apply_runtime_context(config: Any) -> list[tuple[ContextVar[Any], Token[Any]]]:
    trajectory_batch_id = getattr(config, "trajectory_batch_id", None)
    campaign_id = getattr(config, "campaign_id", None)
    world_id = getattr(config, "world_id", None)
    work_unit = trajectory_batch_id or (
        f"{campaign_id}~{world_id}" if campaign_id and world_id else None
    )
    contexts: list[tuple[ContextVar[Any], Any]] = [
        (trajectory_batch_id_ctx, trajectory_batch_id),
        (campaign_id_ctx, campaign_id),
        (work_unit_ctx, work_unit),
        (actor_email_ctx, getattr(config, "studio_actor_email", None)),
        (actor_ip_ctx, getattr(config, "studio_actor_ip", None)),
        (
            impersonation_enabled_ctx,
            bool(getattr(config, "taiga_impersonation_enabled", False)),
        ),
        (
            gateway_routing_enabled_ctx,
            getattr(config, "gateway_routing_enabled", None),
        ),
        (priority_ctx, getattr(config, "resolved_priority", None)),
    ]
    return [(context, context.set(value)) for context, value in contexts]


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content)


def _arguments_object(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments)
        return value if isinstance(value, dict) else {"_value": value}
    except Exception:
        return {"_raw": str(arguments)}


def _call_metrics(call: dict[str, Any] | None) -> dict[str, Any] | None:
    if not call:
        return None
    metrics: dict[str, Any] = {
        key: call.get(key)
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens")
        if call.get(key) is not None
    }
    extra = {
        key: call[key]
        for key in ("cache_creation_tokens", "reasoning_tokens", "total_tokens")
        if key in call
    }
    if extra:
        metrics["extra"] = extra
    return metrics or None


def _native_to_atif(native: dict[str, Any], trajectory_id: str) -> dict[str, Any]:
    messages = native.get("messages") or []
    call_log = (native.get("usage") or {}).get("call_log") or []
    steps: list[dict[str, Any]] = []
    assistant_index = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role in {"system", "user"}:
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "source": role,
                    "message": _text(message.get("content")),
                }
            )
            index += 1
            continue
        if role == "assistant":
            step: dict[str, Any] = {
                "step_id": len(steps) + 1,
                "source": "agent",
                "message": _text(message.get("content")),
            }
            tool_calls = []
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                tool_calls.append(
                    {
                        "tool_call_id": tool_call.get("id") or function.get("name"),
                        "function_name": function.get("name"),
                        "arguments": _arguments_object(function.get("arguments")),
                    }
                )
            if tool_calls:
                step["tool_calls"] = tool_calls
            metrics = _call_metrics(
                call_log[assistant_index] if assistant_index < len(call_log) else None
            )
            if metrics:
                step["metrics"] = metrics
            assistant_index += 1
            results = []
            next_index = index + 1
            while (
                next_index < len(messages)
                and messages[next_index].get("role") == "tool"
            ):
                tool_message = messages[next_index]
                results.append(
                    {
                        "source_call_id": tool_message.get("tool_call_id"),
                        "content": _text(tool_message.get("content")),
                    }
                )
                next_index += 1
            if results:
                step["observation"] = {"results": results}
            steps.append(step)
            index = next_index
            continue
        steps.append(
            {
                "step_id": len(steps) + 1,
                "source": "agent",
                "message": _text(message.get("content")),
                "extra": {"native_role": role},
            }
        )
        index += 1

    usage = native.get("usage") or {}
    final_metrics = {
        key: value
        for key, value in {
            "total_prompt_tokens": usage.get("prompt_tokens"),
            "total_completion_tokens": usage.get("completion_tokens"),
            "total_cached_tokens": usage.get("cached_tokens"),
            "total_steps": len(steps),
        }.items()
        if value is not None
    }
    return {
        "schema_version": "ATIF-v1.5",
        "session_id": trajectory_id,
        "agent": {"name": "archipelago", "version": "studio"},
        "steps": steps,
        "final_metrics": final_metrics,
        "extra": {
            "converted_from": "archipelago-native",
            "native_status": native.get("status"),
            "time_elapsed_sec": native.get("time_elapsed"),
            "native_output": native.get("output"),
        },
    }


class StudioArchipelagoAgent(
    BaseAgent  # pyright: ignore[reportGeneralTypeIssues]
):  # type: ignore[misc]
    """Run Studio's pinned Archipelago harness inside Harbor's trial lifecycle."""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: str | Path,
        model_name: str | None = None,
        logger: Any | None = None,
        mcp_servers: Any | None = None,
        skills_dir: str | None = None,
        *,
        trajectory_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            logger=logger,
            mcp_servers=mcp_servers,
            skills_dir=skills_dir,
            **kwargs,
        )
        self.trajectory_id = trajectory_id

    @staticmethod
    def name() -> str:
        return "studio-archipelago"

    def version(self) -> str:
        return "1.0"

    async def setup(self, environment: Any) -> None:
        """No in-sandbox install is needed; Studio runs the agent host-side."""

        del environment

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        del instruction
        config = await fetch_agent_config(self.trajectory_id)
        setup_logger()
        context_tokens = _apply_runtime_context(config)
        gateway_base = environment.sandbox_url.rstrip("/")
        rest_services = [
            {
                "name": name,
                "base_url": f"{gateway_base}/rest/{name}",
                "openapi_path": "/openapi.json",
            }
            for name in config.rest_service_names
        ]
        try:
            with logger.contextualize(
                trajectory_id=self.trajectory_id,
                execution_framework="harbor",
            ):
                output = await run_archipelago_agent(
                    trajectory_id=self.trajectory_id,
                    initial_messages=config.initial_messages,
                    mcp_gateway_url=f"{gateway_base}/mcp/",
                    mcp_gateway_auth_token=environment.auth_token,
                    mcp_gateway_actor_id=None,
                    agent_config=config.agent_config,
                    orchestrator_model=config.orchestrator_model,
                    orchestrator_extra_args=config.orchestrator_extra_args,
                    parent_trajectory_output=config.parent_trajectory_output,
                    custom_args=config.custom_args,
                    task_custom_fields=config.task_custom_fields,
                    inner_agent_config=config.inner_agent_config,
                    rest_services=rest_services,
                )
        finally:
            for runtime_context, token in reversed(context_tokens):
                runtime_context.reset(token)
            await teardown_logger()
        native = output.model_dump(mode="json")
        logs_dir = Path(self.logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "trajectory.native.json").write_text(
            json.dumps(native, indent=2), encoding="utf-8"
        )
        (logs_dir / "trajectory.json").write_text(
            json.dumps(_native_to_atif(native, self.trajectory_id), indent=2),
            encoding="utf-8",
        )
        context.metadata = {
            "trajectory_id": self.trajectory_id,
            "status": native.get("status"),
        }
