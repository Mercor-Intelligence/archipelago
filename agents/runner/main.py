"""
Main orchestrator for running agents.
"""

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, cast

from loguru import logger

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
    LitellmInputMessage,
)
from runner.agents.registry import get_agent_impl
from runner.models import AgentConfig
from runner.utils.decorators import (
    agent_id_ctx,
    agent_version_ctx,
    orchestrator_id_ctx,
    orchestrator_version_ctx,
)
from runner.utils.logging.main import setup_logger, teardown_logger
from runner.utils.settings import get_settings

# Carries the actor id into the runner without putting it on argv. The
# coordinator sets it when it spawns a virtual coworker; keep the name in step
# with ``MCP_GATEWAY_ACTOR_ID_ENV`` in the environment coordinator's store.
MCP_GATEWAY_ACTOR_ID_ENV = "MCP_GATEWAY_ACTOR_ID"

# from runner.save.main import save_results


async def main(
    trajectory_id: str,
    initial_messages: list[dict[str, Any]],
    mcp_gateway_url: str | None,
    mcp_gateway_auth_token: str | None,
    agent_config: AgentConfig,
    orchestrator_model: str,
    orchestrator_extra_args: dict[str, Any] | None,
    mcp_gateway_actor_id: str | None = None,
    parent_trajectory_output: dict[str, Any] | None = None,
    custom_args: dict[str, Any] | None = None,
    task_custom_fields: dict[str, Any] | None = None,
    inner_agent_config: dict[str, Any] | None = None,
    rest_services: list[dict[str, Any]] | None = None,
    agent_id: str | None = None,
    agent_version: int | None = None,
    orchestrator_id: str | None = None,
    orchestrator_version: int | None = None,
) -> AgentTrajectoryOutput:
    """
    Main entry point for running an agent.

    Args:
        trajectory_id: The trajectory ID being executed
        initial_messages: Initial conversation messages for the agent
        mcp_gateway_url: URL of the MCP gateway on the environment sandbox
        mcp_gateway_auth_token: Optional real bearer token for gateway authentication
        mcp_gateway_actor_id: Optional runtime actor ID for user tenancy
        agent_config: The agent configuration (defn_id + config values)
        orchestrator_model: The LLM model to use (e.g. "anthropic/claude-3-5-sonnet")
        orchestrator_extra_args: Extra arguments for the LLM (e.g. temperature)
        parent_trajectory_output: Structured output from parent trajectory (for continuations)

    Returns:
        AgentTrajectoryOutput with status, messages, and metrics
    """
    settings = get_settings()
    agent_impl = get_agent_impl(agent_config.agent_config_id)

    run_input = AgentRunInput(
        trajectory_id=trajectory_id,
        initial_messages=cast(list[LitellmInputMessage], initial_messages),
        mcp_gateway_url=mcp_gateway_url,
        mcp_gateway_auth_token=mcp_gateway_auth_token,
        mcp_gateway_actor_id=mcp_gateway_actor_id,
        orchestrator_model=orchestrator_model,
        orchestrator_extra_args=orchestrator_extra_args,
        agent_config_values=agent_config.agent_config_values,
        parent_trajectory_output=parent_trajectory_output,
        custom_args=custom_args,
        task_custom_fields=task_custom_fields,
        inner_agent_config=inner_agent_config,
        rest_services=rest_services or [],
    )

    # Publish the agent + orchestrator identity into the context so `build_mcp_gateway_schema`
    # can stamp it onto the MCP gateway client config as request headers, letting the
    # environment gateway attribute each mediated tool call to this agent revision. Set
    # unconditionally: a None here just means the header is omitted downstream (CLI / older
    # server), matching the log-attribution behaviour below.
    agent_id_ctx.set(agent_id)
    agent_version_ctx.set(agent_version)
    orchestrator_id_ctx.set(orchestrator_id)
    orchestrator_version_ctx.set(orchestrator_version)

    # Bind the agent + orchestrator identity (with SCD versions) into the log context so
    # every per-step trajectory log (tool calls, reasoning, final answer) is attributable to
    # the specific agent revision. It reaches Datadog via the whole-`extra` forward (the one
    # sink that can't join back to Postgres); the durable sinks (trajectory_logs / S3 / Redis)
    # deliberately drop these keys — they're constant per trajectory and joinable on
    # trajectory_id (see logging/extra_filter.py). Bind only the ids we actually have, so the
    # CLI / k8s paths that don't pass them don't write null keys onto every record.
    attribution = {
        key: value
        for key, value in (
            ("agent_id", agent_id),
            ("agent_version", agent_version),
            ("orchestrator_id", orchestrator_id),
            ("orchestrator_version", orchestrator_version),
        )
        if value is not None
    }
    with logger.contextualize(trajectory_id=trajectory_id, **attribution):
        logger.info(
            f"Running model {orchestrator_model} with agent {agent_config.agent_name}"
        )

        try:
            async with asyncio.timeout(settings.AGENT_TIMEOUT_SECONDS):
                output = await agent_impl(run_input)
        except TimeoutError:
            logger.error(
                f"Agent timed out after {settings.AGENT_TIMEOUT_SECONDS} seconds"
            )
            output = AgentTrajectoryOutput(
                messages=[],
                status=AgentStatus.ERROR,
                time_elapsed=float(settings.AGENT_TIMEOUT_SECONDS),
            )
        except asyncio.CancelledError:
            logger.error("Agent was cancelled externally")
            output = AgentTrajectoryOutput(
                messages=[],
                status=AgentStatus.CANCELLED,
                time_elapsed=0.0,
            )
        except Exception as e:
            logger.error(f"Error running agent: {repr(e)}")
            output = AgentTrajectoryOutput(
                messages=[],
                status=AgentStatus.ERROR,
                time_elapsed=0.0,
            )

        logger.info(f"Agent run finished with status {output.status}")

        # save_results(trajectory_id, output, None)

        return output


async def run_and_persist(
    *,
    run: Callable[[], Awaitable[AgentTrajectoryOutput]],
    persist: Callable[[AgentTrajectoryOutput], None],
    teardown: Callable[[], Awaitable[None]],
) -> AgentTrajectoryOutput:
    """Run the agent, save its result, then tear down telemetry.

    The result is persisted before teardown, and a failing teardown is
    swallowed: shipping logs is not what the run is for, and a run that
    finished its work must not be discarded because its telemetry could not
    be flushed.
    """
    try:
        result = await run()
        persist(result)
        return result
    finally:
        try:
            await teardown()
        except Exception as e:
            logger.warning(f"Logger teardown failed, ignoring: {e!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run agent")
    parser.add_argument("--trajectory-id", type=str, required=True)
    parser.add_argument(
        "--initial-messages",
        type=str,
        required=True,
        help="Path to JSON file with initial messages",
    )
    parser.add_argument("--mcp-gateway-url", type=str, required=True)
    parser.add_argument(
        "--mcp-gateway-auth-token",
        type=str,
        default="",
        help="Real bearer token for authenticated gateway runtimes",
    )
    # Prefer MCP_GATEWAY_ACTOR_ID over this flag: argv lands in
    # /proc/<pid>/cmdline, which is world-readable, so every sibling actor on
    # the box can read another's identity off it. The flag stays for callers
    # that already pass it.
    parser.add_argument(
        "--mcp-gateway-actor-id",
        type=str,
        default="",
        help="Runtime actor ID for user tenancy",
    )
    parser.add_argument(
        "--agent-config",
        type=str,
        required=True,
        help="Path to JSON file with TrajectoryAgentConfig",
    )
    parser.add_argument("--orchestrator-model", type=str, required=True)
    parser.add_argument(
        "--orchestrator-extra-args",
        type=str,
        help="Path to JSON file with extra args (optional)",
    )
    parser.add_argument(
        "--parent-trajectory-output",
        type=str,
        help="Path to JSON file with parent trajectory output (optional, for continuations)",
    )
    parser.add_argument(
        "--custom-args",
        type=str,
        help="Path to JSON file with custom args (optional)",
    )
    parser.add_argument(
        "--task-custom-fields",
        type=str,
        help="Path to JSON file with task custom fields (optional)",
    )
    parser.add_argument(
        "--inner-agent-config",
        type=str,
        help="Path to JSON file with resolved inner agent config (optional)",
    )
    parser.add_argument("--output", type=str, help="Path to save output JSON")

    args = parser.parse_args()

    with open(args.initial_messages) as f:
        initial_messages = json.load(f)

    with open(args.agent_config) as f:
        agent_config = AgentConfig.model_validate_json(f.read())

    orchestrator_extra_args = None
    if args.orchestrator_extra_args:
        with open(args.orchestrator_extra_args) as f:
            orchestrator_extra_args = json.load(f)

    parent_trajectory_output = None
    if args.parent_trajectory_output:
        with open(args.parent_trajectory_output) as f:
            parent_trajectory_output = json.load(f)

    custom_args = None
    if args.custom_args:
        with open(args.custom_args) as f:
            custom_args = json.load(f)

    task_custom_fields = None
    if args.task_custom_fields:
        with open(args.task_custom_fields) as f:
            task_custom_fields = json.load(f)

    inner_agent_config = None
    if args.inner_agent_config:
        with open(args.inner_agent_config) as f:
            inner_agent_config = json.load(f)

    auth_token = args.mcp_gateway_auth_token or None
    actor_id = (
        args.mcp_gateway_actor_id or os.environ.get(MCP_GATEWAY_ACTOR_ID_ENV) or None
    )

    def write_output(result: AgentTrajectoryOutput) -> None:
        if not args.output:
            return
        with open(args.output, "w") as f:
            f.write(result.model_dump_json(indent=2))

    async def run_cli() -> AgentTrajectoryOutput:
        setup_logger()
        return await run_and_persist(
            run=lambda: main(
                trajectory_id=args.trajectory_id,
                initial_messages=initial_messages,
                mcp_gateway_url=args.mcp_gateway_url,
                mcp_gateway_auth_token=auth_token,
                mcp_gateway_actor_id=actor_id,
                agent_config=agent_config,
                orchestrator_model=args.orchestrator_model,
                orchestrator_extra_args=orchestrator_extra_args,
                parent_trajectory_output=parent_trajectory_output,
                custom_args=custom_args,
                task_custom_fields=task_custom_fields,
                inner_agent_config=inner_agent_config,
            ),
            persist=write_output,
            teardown=lambda: teardown_logger(trajectory_id=args.trajectory_id),
        )

    asyncio.run(run_cli())
