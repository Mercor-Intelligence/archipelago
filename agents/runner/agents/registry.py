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

def _eq_filter(field_id: str, value: str) -> dict[str, Any]:
    return {"predicate_custom_field_id": field_id, "comparison": "eq", "value": value}

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
                field_id="accounting_mode",
                field_type=TaskFieldType.SELECT,
                label="Accounting Mode",
                description=(
                    "Which per-step $/token budget-warning mechanism is active "
                    "for this run: Default (none), 90/10 Token Accounting (Token "
                    "Budget below), or Full Cost Accounting (real $ cost "
                    "tracking further below). Mutually exclusive. Agents "
                    "configured before this field existed that already have a "
                    "Token Budget set keep working under 90/10 Token Accounting "
                    "automatically. Turn Warnings (below) are independent of "
                    "this selector and apply in any mode."
                ),
                options=["default", "token_accounting", "cost_accounting"],
                default_value="default",
            ),
            TaskFieldSchema(
                field_id="token_budget",
                field_type=TaskFieldType.NUMBER,
                label="Token Budget",
                description=(
                    "Total provider-reported prompt+completion tokens the run "
                    "may spend. The agent is told the remaining budget each "
                    "step and gets one final step once it is exhausted. "
                    "0 disables budgeting. Only used in 90/10 Token Accounting."
                ),
                default_value=0,
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "token_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="turn_warnings_enabled",
                field_type=TaskFieldType.BOOLEAN,
                label="Enable Turn Warnings",
                description=(
                    "Inject a per-step 'N step(s) remaining' warning so the "
                    "agent wraps up before hitting Max Steps. Off by default. "
                    "Independent of Accounting Mode/Token Budget — applies in "
                    "any mode, including Full Cost Accounting."
                ),
                default_value=False,
            ),
            TaskFieldSchema(
                field_id="input_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Input Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's input rate for this "
                    "run. Only used when explicitly set; otherwise the table "
                    "default applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="output_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Output Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's output rate for "
                    "this run. Only used when explicitly set; otherwise the "
                    "table default applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cached_input_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Cached Input Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's cache-read rate for "
                    "this run. Only used when explicitly set; otherwise the "
                    "table default (or a 10% of input rate heuristic) applies. "
                    "Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cache_creation_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Cache Creation Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's cache-write rate "
                    "for this run. Only used when explicitly set; otherwise "
                    "the table default (or a 125% of input rate heuristic) "
                    "applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cost_budget_usd",
                field_type=TaskFieldType.NUMBER,
                label="Cost Budget (USD)",
                description=(
                    "Total USD the run may spend. The agent is told the "
                    "remaining budget each step and gets one final step once "
                    "it is exhausted. 0 disables the cap, but cost is still "
                    "logged. Only used in Full Cost Accounting."
                ),
                default_value=0,
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
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
