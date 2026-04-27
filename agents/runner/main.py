"""
Main orchestrator for running agents.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, cast

from loguru import logger

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
    LitellmInputMessage,
)
from runner.agents.registry import get_agent_impl
from runner.manifest import RunManifest
from runner.models import AgentConfig
from runner.utils.settings import get_settings

# from runner.save.main import save_results


async def main(
    trajectory_id: str,
    initial_messages: list[dict[str, Any]],
    mcp_gateway_url: str | None,
    mcp_gateway_auth_token: str | None,
    agent_config: AgentConfig,
    orchestrator_model: str,
    orchestrator_extra_args: dict[str, Any] | None,
    parent_trajectory_output: dict[str, Any] | None = None,
    custom_args: dict[str, Any] | None = None,
) -> AgentTrajectoryOutput:
    """
    Main entry point for running an agent.

    Args:
        trajectory_id: The trajectory ID being executed
        initial_messages: Initial conversation messages for the agent
        mcp_gateway_url: URL of the MCP gateway on the environment sandbox
        mcp_gateway_auth_token: Bearer token for MCP gateway authentication
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
        orchestrator_model=orchestrator_model,
        orchestrator_extra_args=orchestrator_extra_args,
        agent_config_values=agent_config.agent_config_values,
        parent_trajectory_output=parent_trajectory_output,
        custom_args=custom_args,
    )

    with logger.contextualize(trajectory_id=trajectory_id):
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
        help="Bearer token for MCP gateway (empty for local/unauthenticated)",
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
    parser.add_argument("--output", type=str, help="Path to save output JSON")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed passed to LiteLLM. Best-effort, provider-dependent. "
        "Captured in run_manifest.json regardless.",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        default=False,
        help="Force temperature=0 in orchestrator_extra_args and require "
        "--seed. Best-effort determinism. Captured in run_manifest.json.",
    )

    args = parser.parse_args()

    if args.deterministic and args.seed is None:
        parser.error("--deterministic requires --seed")

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

    auth_token = args.mcp_gateway_auth_token or None

    # Merge seed and deterministic flags into extra_args (preserve existing)
    if orchestrator_extra_args is None:
        orchestrator_extra_args = {}
    if args.seed is not None:
        orchestrator_extra_args.setdefault("seed", args.seed)
    if args.deterministic:
        orchestrator_extra_args["temperature"] = 0

    result = asyncio.run(
        main(
            trajectory_id=args.trajectory_id,
            initial_messages=initial_messages,
            mcp_gateway_url=args.mcp_gateway_url,
            mcp_gateway_auth_token=auth_token,
            agent_config=agent_config,
            orchestrator_model=args.orchestrator_model,
            orchestrator_extra_args=orchestrator_extra_args,
            parent_trajectory_output=parent_trajectory_output,
            custom_args=custom_args,
        )
    )

    if args.output:
        with open(args.output, "w") as f:
            f.write(result.model_dump_json(indent=2))

        # Write reproducibility manifest next to trajectory output
        manifest = RunManifest.from_run_inputs(
            trajectory_id=args.trajectory_id,
            agent_config_id=agent_config.agent_config_id,
            agent_config_values=agent_config.agent_config_values,
            orchestrator_model=args.orchestrator_model,
            orchestrator_extra_args=orchestrator_extra_args,
            seed=args.seed,
            deterministic=args.deterministic,
            # TODO: mcp_server_configs not available in CLI scope;
            # populate when orchestrator exposes them
            mcp_server_configs=None,
        )
        manifest_path = Path(args.output).with_suffix(".manifest.json")
        manifest.write(manifest_path)
        logger.info(f"Wrote run manifest to {manifest_path}")
    else:
        logger.info(
            "Manifest skipped: --output not set, nowhere to write sidecar"
        )
