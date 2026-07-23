"""
Agent registry mapping agent IDs to their implementations and config schemas.
"""

from typing import Any

from runner.agents.loop_agent.main import run as loop_agent_run
from runner.agents.models import (
    AgentConfigIds,
    AgentDefn,
    AgentImpl,
    AgentRunInput,
    AgentTrajectoryOutput,
)
from runner.agents.react_toolbelt_agent.main import run as react_toolbelt_agent_run
from runner.models import TaskFieldSchema, TaskFieldType

AGENT_REGISTRY: dict[AgentConfigIds, AgentDefn] = {
    AgentConfigIds.LOOP_AGENT: AgentDefn(
        agent_config_id=AgentConfigIds.LOOP_AGENT,
        agent_impl=loop_agent_run,
        agent_config_fields=[
            TaskFieldSchema(
                field_id="timeout",
                field_type=TaskFieldType.NUMBER,
                label="Timeout (seconds)",
                description="Maximum time for agent execution",
                default_value=10800,  # 3 hours
                min_value=300,  # 5 minutes
                max_value=28800,  # 8 hours
            ),
            TaskFieldSchema(
                field_id="max_steps",
                field_type=TaskFieldType.NUMBER,
                label="Max Steps",
                description="Maximum number of LLM calls before stopping",
                default_value=100,
                min_value=1,
                max_value=1000,
            ),
            TaskFieldSchema(
                field_id="tool_call_timeout",
                field_type=TaskFieldType.NUMBER,
                label="Tool Call Timeout (seconds)",
                description="Timeout for individual tool calls",
                default_value=60,
                min_value=10,
                max_value=600,
            ),
            TaskFieldSchema(
                field_id="llm_response_timeout",
                field_type=TaskFieldType.NUMBER,
                label="LLM Response Timeout (seconds)",
                description="Timeout for LLM API calls",
                default_value=600,
                min_value=30,
                max_value=1200,
            ),
            TaskFieldSchema(
                field_id="token_budget",
                field_type=TaskFieldType.NUMBER,
                label="Token Budget",
                description=(
                    "Total provider-reported prompt+completion tokens the run "
                    "may spend. The agent is told the remaining budget each "
                    "step and gets one final step once it is exhausted. "
                    "0 disables budgeting."
                ),
                default_value=0,
                min_value=0,
            ),
            TaskFieldSchema(
                field_id="turn_warnings_enabled",
                field_type=TaskFieldType.BOOLEAN,
                label="Enable Turn Warnings",
                description=(
                    "Inject a per-step 'N step(s) remaining' warning so the "
                    "agent wraps up before hitting Max Steps. Off by default; "
                    "independent of Token Budget."
                ),
                default_value=False,
            ),
        ],
    ),
    AgentConfigIds.REACT_TOOLBELT_AGENT: AgentDefn(
        agent_config_id=AgentConfigIds.REACT_TOOLBELT_AGENT,
        agent_impl=react_toolbelt_agent_run,
        agent_config_fields=[
            TaskFieldSchema(
                field_id="timeout",
                field_type=TaskFieldType.NUMBER,
                label="Timeout (seconds)",
                description="Maximum time for agent execution",
                default_value=10800,  # 3 hours
                min_value=300,  # 5 minutes
                max_value=28800,  # 8 hours
            ),
            TaskFieldSchema(
                field_id="max_steps",
                field_type=TaskFieldType.NUMBER,
                label="Max Steps",
                description="Maximum number of LLM calls before stopping",
                default_value=250,
                min_value=1,
                max_value=1000,
            ),
            TaskFieldSchema(
                field_id="llm_response_timeout",
                field_type=TaskFieldType.NUMBER,
                label="LLM Response Timeout (seconds)",
                description="Timeout for a single LLM API call",
                default_value=600,
                min_value=30,
                max_value=10800,
            ),
        ],
    ),
}

def get_agent_impl(agent_config_id: str) -> AgentImpl:
    """
    Get the agent implementation function for the given agent config ID.

    Args:
        agent_config_id: The agent config ID to look up (e.g., "loop_agent")

    Returns:
        The agent implementation function

    Raises:
        ValueError: If the agent config ID is not found in the registry
    """
    try:
        config_id_enum = AgentConfigIds(agent_config_id)
    except ValueError as e:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}") from e

    defn = AGENT_REGISTRY.get(config_id_enum)
    if defn is None:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}")

    if defn.agent_impl is None:
        raise ValueError(
            f"Agent '{agent_config_id}' is registered but has no implementation"
        )

    return defn.agent_impl

def get_agent_defn(agent_config_id: str) -> AgentDefn:
    """
    Get the full agent definition for the given agent config ID.

    Args:
        agent_config_id: The agent config ID to look up (e.g., "loop_agent")

    Returns:
        The agent definition including config fields

    Raises:
        ValueError: If the agent config ID is not found in the registry
    """
    try:
        config_id_enum = AgentConfigIds(agent_config_id)
    except ValueError as e:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}") from e

    defn = AGENT_REGISTRY.get(config_id_enum)
    if defn is None:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}")

    return defn
